"""End-to-end execution recorder for agent/pipeline runs (M17).

The recorder captures every step of an agent/pipeline run (not just the
coarse job stats) so the data is reusable for analytics and the /agent
page can show the actual sequence of nodes that fired.

Two layers:

* :class:`AgentRecorder` — the high-level entry point. One recorder per
  run. ``start(pipeline_id, ...)`` creates the :class:`AgentRun` row and
  returns the recorder; ``step(node_id, ...)`` enters a context manager
  for one node; ``finish()`` updates the run with aggregate counters and
  status. Designed to wrap a pipeline from outside (the entry-point
  API/handler creates the recorder; inner steps call ``recorder.step()``).

* :class:`agent_step` — a re-exported context manager that calls
  ``recorder.step(...)`` and handles success/failure. Used by the inner
  pipeline functions that don't already hold a recorder reference
  (they fetch it from the module-level ``current_recorder()`` set by the
  entry point via :func:`set_current_recorder`).

Usage from an entry-point handler::

    def _run_job(session, cfg, *, job_id, paper_id):
        rec = AgentRecorder(session, pipeline_id="process", pipeline_name="Process paper")
        rec.start(context={"paper_id": paper_id}, paper_id=paper_id, job_id=job_id)
        token = set_current_recorder(rec)
        try:
            with rec.step("download", label="Download PDF", kind="step"):
                _step_download(session, cfg, paper)
            with rec.step("parse", label="MinerU parse to MD", kind="step"):
                _step_parse(session, cfg, paper)
            # ...
        finally:
            clear_current_recorder(token)
            rec.finish(summary={"ok": True})

Usage from an inner step that doesn't want to thread the recorder through::

    with agent_step("summarize", label="summarize", kind="llm", feature="summarize") as s:
        data = llm.chat_json(...)
        s.set_output(json.dumps(data)[:500])
        s.set_tokens(model=model, prompt_tokens=..., completion_tokens=..., total_tokens=...)

The recorder is **best-effort**: a DB error during a step write logs a
warning and skips the write rather than aborting the pipeline. The
``AgentRun`` row is the one that has to land, because it gives the UI
the existence proof; the steps are the nice-to-have detail.

Recorder and step commits piggy-back on the caller's session. A step is
written at *enter* (status=running) and updated at *exit* (success/failed).
``recorder.finish()`` commits the run's final state. If the caller's
transaction later rolls back, the recorder's commits would already be
persistent — this is consistent with the existing token-usage recorder
in :mod:`carrel.usage`, and avoids opening a second connection (which
deadlocks against the in-memory SQLite used in tests).
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session

from carrel.models import (
    AgentRun,
    AgentRunStatus,
    AgentStep,
    AgentStepStatus,
)

logger = logging.getLogger(__name__)


# Cap on the size of free-form text columns so a runaway pipeline doesn't
# blow up the row size (Postgres TOAST is fine but indexes / DDL get
# awkward past ~10KB, and a single step is rarely that interesting).
_MAX_TEXT_BYTES = 4_000
_MAX_DETAIL_BYTES = 32_000


def _truncate(text: str | None, *, limit: int = _MAX_TEXT_BYTES) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…[truncated]"


def _truncate_json(obj: Any, *, limit: int = _MAX_DETAIL_BYTES) -> Any:
    """Truncate large JSON values (strings inside the tree).

    Non-string values are kept as-is; only string leaves are trimmed so
    the JSON shape stays parseable. We don't bother with depth limits
    because recorder callers are our own code.
    """
    if isinstance(obj, str):
        return _truncate(obj, limit=limit)
    if isinstance(obj, list):
        return [_truncate_json(v, limit=limit) for v in obj]
    if isinstance(obj, dict):
        return {k: _truncate_json(v, limit=limit) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# Step context manager
# ---------------------------------------------------------------------------


class _StepCtx:
    """A single step's mutable handle, returned by ``recorder.step(...)``.

    Caller can call ``.set_input(...)`` / ``.set_output(...)`` /
    ``.set_detail(...)`` / ``.set_tokens(...)`` *inside* the with-block;
    the values land on the row when the block exits.
    """

    def __init__(self, recorder: "AgentRecorder", step: AgentStep, seq: int) -> None:
        self._recorder = recorder
        self._step = step
        self._seq = seq
        self._exited = False
        self._error: BaseException | None = None

    @property
    def step_id(self) -> int | None:
        return self._step.id

    # -- context manager --------------------------------------------------

    def __enter__(self) -> "_StepCtx":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            msg = f"{type(exc).__name__}: {exc}".strip() or type(exc).__name__
            self._finalize(status=AgentStepStatus.failed.value, error=msg)
        else:
            self._finalize(status=AgentStepStatus.success.value, error=None)
        return False  # never swallow

    # -- mutators (usable inside the with block) ---------------------------

    def set_input(self, text: str | None) -> None:
        self._step.input_summary = _truncate(text)

    def set_output(self, text: str | None) -> None:
        self._step.output_summary = _truncate(text)

    def set_detail(self, detail: Any) -> None:
        if detail is None:
            return
        try:
            self._step.detail = _truncate_json(detail)
        except TypeError:
            # Non-JSON-serializable; coerce to string and store.
            self._step.detail = {"_raw": _truncate(str(detail))}

    def set_tokens(
        self,
        *,
        model: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> None:
        if model is not None:
            self._step.model = model[:120]
        if prompt_tokens is not None:
            self._step.prompt_tokens = int(prompt_tokens)
        if completion_tokens is not None:
            self._step.completion_tokens = int(completion_tokens)
        if total_tokens is not None:
            self._step.total_tokens = int(total_tokens)
        # Reflect in the cached totals on the run too (no-op until finish()).
        self._recorder._accumulate_tokens(
            prompt_tokens or 0, completion_tokens or 0, total_tokens or 0
        )

    # Internal: flush to DB. Called by the context-manager exit.
    def _finalize(self, *, status: str, error: str | None) -> None:
        if self._exited:
            return
        self._exited = True
        self._step.status = status
        self._step.error = _truncate(error) if error else None
        self._step.finished_at = datetime.now(UTC)
        started = self._step.started_at
        if started is not None:
            # SQLite (and some Postgres round-trips) drops tzinfo on
            # retrieval even when the value was stored as a tz-aware
            # datetime. Strip the tz from both sides so the subtraction
            # never raises a "can't subtract naive and aware" error.
            if started.tzinfo is None and self._step.finished_at.tzinfo is not None:
                started = started.replace(tzinfo=self._step.finished_at.tzinfo)
            self._step.duration_ms = max(
                0, int((self._step.finished_at - started).total_seconds() * 1000)
            )
        try:
            self._recorder._session.add(self._step)
            self._recorder._session.commit()
            self._recorder._session.refresh(self._step)
        except Exception as e:  # noqa: BLE001 - recorder must not break the pipeline
            logger.warning("agent_step finalize failed: %s", e)
            try:
                self._recorder._session.rollback()
            except Exception:  # noqa: BLE001
                pass
        # Bump run counters even if the row write failed; finish() will
        # only write the counters that already made it to the in-memory
        # row.
        self._recorder._accumulate_step(status)


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


class AgentRecorder:
    """Wraps one :class:`AgentRun` and provides a ``step(...)`` context manager.

    A recorder owns the session it was given and the run row it created;
    caller is expected to keep the session alive for the duration of the
    run (the pipeline functions use it anyway).
    """

    def __init__(
        self,
        session: Session,
        *,
        pipeline_id: str,
        pipeline_name: str,
        trigger: str = "manual",
    ) -> None:
        self._session = session
        self._pipeline_id = pipeline_id
        self._pipeline_name = pipeline_name
        self._trigger = trigger
        self._run: AgentRun | None = None
        self._seq_lock = threading.Lock()
        self._seq = 0
        # Aggregate counters; flushed in finish().
        self._success_count = 0
        self._failed_count = 0
        self._skipped_count = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0

    # -- lifecycle ---------------------------------------------------------

    def start(
        self,
        *,
        context: dict[str, Any] | None = None,
        job_id: int | None = None,
        paper_id: str | None = None,
        subject: str | None = None,
    ) -> AgentRun:
        """Insert the run row (status=running). Idempotent on this instance."""
        if self._run is not None:
            return self._run
        run = AgentRun(
            pipeline_id=self._pipeline_id,
            pipeline_name=self._pipeline_name,
            status=AgentRunStatus.running.value,
            trigger=self._trigger,
            context=_truncate_json(context) if context else None,
            job_id=job_id,
            paper_id=paper_id,
            subject=subject,
            started_at=datetime.now(UTC),
        )
        self._session.add(run)
        try:
            self._session.commit()
            self._session.refresh(run)
        except Exception as e:  # noqa: BLE001
            logger.warning("agent_run start failed: %s", e)
            try:
                self._session.rollback()
            except Exception:  # noqa: BLE001
                pass
            # Continue with an in-memory row so the rest of the pipeline
            # still gets recorder-level ergonomics; finish() will retry
            # the commit.
        self._run = run
        return run

    @property
    def run_id(self) -> int | None:
        return self._run.id if self._run is not None else None

    @property
    def run(self) -> AgentRun | None:
        return self._run

    def step(  # noqa: PLR0913 - public API; keeping the kwargs is friendlier
        self,
        node_id: str | None,
        *,
        label: str,
        kind: str = "step",
        feature: str | None = None,
        detail: Any = None,
    ) -> "_StepCtx":
        """Enter a step context. Returns a :class:`_StepCtx` for set_* calls."""
        if self._run is None:
            # start() failed earlier; still emit a context manager that
            # is a no-op so the caller's `with` block doesn't crash.
            class _Noop:
                def __enter__(self_inner):  # noqa: N805
                    return self

                def __exit__(self_inner, *exc):  # noqa: N805
                    return False

                def set_input(self_inner, _): pass  # noqa: N805

                def set_output(self_inner, _): pass  # noqa: N805

                def set_detail(self_inner, _): pass  # noqa: N805

                def set_tokens(self_inner, **_): pass  # noqa: N805

            return _Noop()  # type: ignore[return-value]
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        step = AgentStep(
            run_id=self._run.id or 0,
            seq=seq,
            node_id=node_id,
            label=label[:200],
            kind=kind if kind in ("step", "llm") else "step",
            feature=feature,
            status=AgentStepStatus.running.value,
            detail=_truncate_json(detail) if detail is not None else None,
            started_at=datetime.now(UTC),
        )
        try:
            self._session.add(step)
            self._session.commit()
            self._session.refresh(step)
        except Exception as e:  # noqa: BLE001
            logger.warning("agent_step insert failed: %s", e)
            try:
                self._session.rollback()
            except Exception:  # noqa: BLE001
                pass
        return _StepCtx(self, step, seq)

    def finish(
        self,
        *,
        status: str | None = None,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Mark the run done. Idempotent. Updates the run row with totals."""
        if self._run is None:
            return
        if status is not None and status in {s.value for s in AgentRunStatus}:
            self._run.status = status
        else:
            # Infer: any failed step means failed; otherwise success.
            self._run.status = (
                AgentRunStatus.failed.value
                if self._failed_count > 0
                else AgentRunStatus.success.value
            )
        self._run.finished_at = datetime.now(UTC)
        self._run.step_count = self._seq
        self._run.success_count = self._success_count
        self._run.failed_count = self._failed_count
        if summary is not None:
            existing = dict(self._run.summary or {})
            existing.update(summary)
            self._run.summary = _truncate_json(existing)
        if self._total_tokens:
            tp = dict(self._run.summary or {})
            tp["total_prompt_tokens"] = (
                tp.get("total_prompt_tokens", 0) + self._prompt_tokens
            )
            tp["total_completion_tokens"] = (
                tp.get("total_completion_tokens", 0) + self._completion_tokens
            )
            tp["total_tokens"] = (
                tp.get("total_tokens", 0) + self._total_tokens
            )
            self._run.summary = _truncate_json(tp)
        if error:
            self._run.error = _truncate(error)
        try:
            self._session.add(self._run)
            self._session.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("agent_run finish failed: %s", e)
            try:
                self._session.rollback()
            except Exception:  # noqa: BLE001
                pass

    # -- internal helpers used by _StepCtx ---------------------------------

    def _accumulate_step(self, status: str) -> None:
        if status == AgentStepStatus.success.value:
            self._success_count += 1
        elif status == AgentStepStatus.failed.value:
            self._failed_count += 1
        elif status == AgentStepStatus.skipped.value:
            self._skipped_count += 1

    def _accumulate_tokens(
        self, prompt: int, completion: int, total: int
    ) -> None:
        self._prompt_tokens += prompt
        self._completion_tokens += completion
        self._total_tokens += total


# ---------------------------------------------------------------------------
# Ambient recorder (ContextVar) — lets inner functions record steps
# without threading the recorder through every signature.
# ---------------------------------------------------------------------------


_current_recorder: ContextVar[AgentRecorder | None] = ContextVar(
    "carrel_current_recorder", default=None
)


def set_current_recorder(rec: AgentRecorder) -> Any:
    """Bind a recorder as the ambient one; returns a token for restore."""
    return _current_recorder.set(rec)


def clear_current_recorder(token: Any) -> None:
    _current_recorder.reset(token)


def current_recorder() -> AgentRecorder | None:
    return _current_recorder.get()


@contextmanager
def agent_step(
    node_id: str | None,
    *,
    label: str,
    kind: str = "step",
    feature: str | None = None,
    detail: Any = None,
) -> Iterator[_StepCtx | "_NoopStep"]:
    """Record a step using the ambient recorder, if one is bound.

    Falls back to a no-op context manager so callers don't have to guard
    every `with` block. The yielded object supports the same set_input /
    set_output / set_detail / set_tokens methods either way.
    """
    rec = current_recorder()
    if rec is None:
        yield _NoopStep()
        return
    ctx = rec.step(node_id, label=label, kind=kind, feature=feature, detail=detail)
    start = time.monotonic()
    try:
        yield ctx
    except BaseException as e:
        # Capture the error message but re-raise so the caller's try/except
        # still runs. We strip the full traceback to one line — the
        # application log already has the rest.
        msg = f"{type(e).__name__}: {e}".strip() or type(e).__name__
        ctx._finalize(status=AgentStepStatus.failed.value, error=msg)  # noqa: SLF001
        raise
    else:
        ctx._finalize(  # noqa: SLF001
            status=AgentStepStatus.success.value, error=None
        )
    finally:
        # ``finally`` runs after both branches; nothing to do here —
        # kept as a hook in case future work needs cleanup that doesn't
        # depend on success/failure.
        _ = start


class _NoopStep:
    """Stand-in when there's no ambient recorder. Same surface as _StepCtx."""

    @property
    def step_id(self) -> None:
        return None

    def set_input(self, _text: str | None) -> None:
        pass

    def set_output(self, _text: str | None) -> None:
        pass

    def set_detail(self, _detail: Any) -> None:
        pass

    def set_tokens(self, **_kw: Any) -> None:
        pass


__all__ = [
    "AgentRecorder",
    "agent_step",
    "set_current_recorder",
    "clear_current_recorder",
    "current_recorder",
    "PIPELINE_CATALOG",
    "pipeline_display_name",
    "run_with_recorder",
]


# ---------------------------------------------------------------------------
# Convenience wrapper for entry-point handlers
# ---------------------------------------------------------------------------


@contextmanager
def run_with_recorder(
    session: Session,
    *,
    pipeline_id: str,
    context: dict[str, Any] | None = None,
    job_id: int | None = None,
    paper_id: str | None = None,
    subject: str | None = None,
    trigger: str = "manual",
) -> Iterator[AgentRecorder]:
    """Bind an :class:`AgentRecorder` for the duration of the block.

    Yields the recorder so callers can attach extra detail/inputs after the
    fact. The recorder is best-effort: any failure inside the block marks
    the run as ``failed`` and is logged; the exception is re-raised so the
    caller's own error handling still runs.

    The recorder is the ambient one for any :func:`agent_step` calls inside
    the block; the ambient binding is cleaned up on exit so a new request
    doesn't see the previous one.
    """
    rec = AgentRecorder(
        session,
        pipeline_id=pipeline_id,
        pipeline_name=pipeline_display_name(pipeline_id),
        trigger=trigger,
    )
    rec.start(
        context=context, job_id=job_id, paper_id=paper_id, subject=subject
    )
    token = set_current_recorder(rec)
    try:
        yield rec
    except BaseException as e:
        rec.finish(
            status=AgentRunStatus.failed.value,
            error=f"{type(e).__name__}: {e}".strip(),
        )
        raise
    else:
        rec.finish(summary=context)
    finally:
        clear_current_recorder(token)


# ---------------------------------------------------------------------------
# Pipeline catalog (mirrors frontend/src/lib/agentPipelines.ts IDs)
# ---------------------------------------------------------------------------


# Pipeline ids and their display names. The display name is cached on
# the AgentRun row at start() so renaming a pipeline in the frontend
# catalog never breaks the historical record. This dict is the
# authoritative server-side list; the frontend has its own richer
# catalog (with icons, descriptions, etc.) that the /agent page merges
# with this one.
PIPELINE_CATALOG: dict[str, str] = {
    "sync": "Sync (discover)",
    "process": "Process paper",
    "embed": "Embed (RAG index)",
    "citations": "Citations (S2)",
    "publication_check": "Publication check",
    "remote_fill": "Remote PDF fill",
    "paper_dedup": "Paper dedup",
    "scholar_dedup": "Scholar dedup",
    "authors_backfill": "Authors backfill",
    "scholar_works_sync": "Scholar works sync",
    "wiki": "Wiki compile",
    "wiki_recompile": "Wiki recompile",
    "paper_extract": "Paper extract",
    "scholar_enrich": "Scholar enrich",
    "paper_chat": "Paper chat",
    "wiki_chat": "Wiki chat",
}


def pipeline_display_name(pipeline_id: str) -> str:
    """Best-effort display name for a pipeline id; falls back to the id."""
    return PIPELINE_CATALOG.get(pipeline_id, pipeline_id)
