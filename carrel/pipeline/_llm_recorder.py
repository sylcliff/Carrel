"""Shared LLM-usage recording helper.

Both :mod:`carrel.pipeline.summarize` and :mod:`carrel.pipeline.topics`
call :func:`carrel.llm.chat_json` with an ``on_usage`` callback. The
callback has to (a) persist the persistent token-usage row via
:mod:`carrel.usage` and (b) pipe the same numbers into the ambient
:class:`AgentRecorder` so the /agent page can show LLM cost alongside
the rest of the run timeline.

The plumbing is identical in both call sites; centralising it keeps the
two pipelines from drifting apart.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlmodel import Session, select

from carrel import usage
from carrel.agent_recorder import current_recorder
from carrel.models import AgentStep, AgentStepStatus

logger = logging.getLogger(__name__)


def make_record_usage_callback(
    session: Session,
    *,
    paper_id: str,
    feature: str,
):
    """Return an ``on_usage`` callback for :func:`carrel.llm.chat_json`.

    The returned callable has signature ``(model, feature, response)`` and:

    1. delegates to :func:`carrel.usage.make_usage_callback` for the
       durable per-call :class:`TokenUsage` row;
    2. when an ambient :class:`AgentRecorder` is bound, finds the most
       recent still-running :class:`AgentStep` on that run and stamps
       the model / token counts onto it.

    Failures in either side-channel are logged and swallowed so a buggy
    recorder can never break an LLM call.
    """

    def _cb(model: str, feature: str, resp: Any) -> None:
        # 1) Persistent token-usage row.
        try:
            base_cb = usage.make_usage_callback(
                session, feature=feature, paper_id=paper_id
            )
            base_cb(model, feature, resp)
        except Exception:  # noqa: BLE001 - recording must never break LLM
            logger.warning("usage callback failed for %s", paper_id)

        # 2) Ambient recorder: stamp tokens onto the running step.
        rec = current_recorder()
        if rec is None:
            return
        try:
            step = session.exec(
                select(AgentStep)
                .where(AgentStep.run_id == rec.run_id)
                .where(AgentStep.status == AgentStepStatus.running.value)
                .order_by(AgentStep.seq.desc())
                .limit(1)
            ).first()
            if step is None:
                return
            usage_obj = getattr(resp, "usage", None)
            if usage_obj is None:
                return
            prompt = getattr(usage_obj, "prompt_tokens", None)
            completion = getattr(usage_obj, "completion_tokens", None)
            total = getattr(usage_obj, "total_tokens", None)
            if prompt is not None:
                step.prompt_tokens = int(prompt)
            if completion is not None:
                step.completion_tokens = int(completion)
            if total is not None:
                step.total_tokens = int(total)
            if model:
                step.model = model[:120]
            session.add(step)
            session.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("recorder token write failed: %s", e)

    return _cb


__all__ = ["make_record_usage_callback"]
