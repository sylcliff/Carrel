"""Token usage recording + aggregation queries.

Each LLM call funnels through :mod:`carrel.llm`, which extracts the
``usage`` block from the litellm response and hands it to
:func:`record_usage` here. The Usage page reads back via the helpers
in this module (totals, by model, by feature, by day, recent rows).
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from sqlalchemy import case, func
from sqlmodel import Session, select

from carrel.models import TokenUsage

logger = logging.getLogger(__name__)


def _safe_int(d: Any, key: str) -> int:
    v = d.get(key) if isinstance(d, dict) else None
    if isinstance(v, bool):
        return 0  # bools are ints in Python; reject them
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str) and v.isdigit():
        return int(v)
    return 0


def extract_usage(resp: Any) -> dict[str, int] | None:
    """Pull ``prompt_tokens``/``completion_tokens``/``total_tokens`` out of a
    litellm ``ModelResponse``. Returns ``None`` if usage is absent (some
    providers omit it on streaming failures)."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    # litellm exposes usage as a pydantic model with .prompt_tokens etc.
    if hasattr(usage, "model_dump"):
        d = usage.model_dump()
    elif hasattr(usage, "dict"):
        d = usage.dict()
    elif isinstance(usage, dict):
        d = usage
    else:
        d = {"prompt_tokens": usage.prompt_tokens,
             "completion_tokens": usage.completion_tokens,
             "total_tokens": usage.total_tokens}
    prompt = _safe_int(d, "prompt_tokens")
    completion = _safe_int(d, "completion_tokens")
    total = _safe_int(d, "total_tokens")
    # Some providers only set total; derive the missing side.
    if total == 0 and (prompt or completion):
        total = prompt + completion
    if prompt == 0 and completion == 0 and total == 0:
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total or (prompt + completion),
    }


def record_usage(
    session: Session,
    *,
    model: str,
    feature: str,
    usage: dict[str, int] | None,
    job_id: int | None = None,
    paper_id: str | None = None,
) -> None:
    """Persist one usage row. Safe to call with ``usage=None`` (no-op).

    Best-effort: a DB error here must not break the LLM call. The caller
    should wrap this in try/except and log on failure.
    """
    if not usage:
        return
    row = TokenUsage(
        model=model,
        feature=feature,
        job_id=job_id,
        paper_id=paper_id,
        prompt_tokens=int(usage.get("prompt_tokens", 0)),
        completion_tokens=int(usage.get("completion_tokens", 0)),
        total_tokens=int(usage.get("total_tokens", 0)),
    )
    session.add(row)


def make_usage_callback(
    session: Session,
    *,
    feature: str,
    job_id: int | None = None,
    paper_id: str | None = None,
) -> Callable[[str, str, Any], None]:
    """Return a closure suitable for ``llm.chat_json(..., on_usage=...)``.

    The closure records a :class:`TokenUsage` row using :func:`record_usage`.
    DB failures are logged and swallowed so the LLM call's return value is
    never blocked by accounting.
    """

    def _cb(model: str, _feature: str, resp: Any) -> None:
        usage = extract_usage(resp)
        if not usage:
            return
        try:
            record_usage(
                session,
                model=model,
                feature=feature,
                usage=usage,
                job_id=job_id,
                paper_id=paper_id,
            )
        except Exception as e:  # noqa: BLE001 - accounting must not break the LLM
            logger.warning("record_usage failed: %s", e)

    return _cb


# ---------------------------------------------------------------------------
# Aggregation queries
# ---------------------------------------------------------------------------


def _since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def summary(session: Session, *, since_days: int | None = None) -> dict[str, Any]:
    """Totals + per-bucket breakdown. ``since_days=None`` returns all-time."""
    base = select(TokenUsage)
    if since_days is not None:
        base = base.where(TokenUsage.created_at >= _since(since_days))
    total = session.exec(
        select(
            func.coalesce(func.sum(TokenUsage.prompt_tokens), 0),
            func.coalesce(func.sum(TokenUsage.completion_tokens), 0),
            func.coalesce(func.sum(TokenUsage.total_tokens), 0),
            func.count(TokenUsage.id),
        ).where(*[c for c in base._where_criteria])  # type: ignore[attr-defined]
    ).one()
    return {
        "prompt_tokens": int(total[0] or 0),
        "completion_tokens": int(total[1] or 0),
        "total_tokens": int(total[2] or 0),
        "calls": int(total[3] or 0),
    }


def _grouped_totals(
    session: Session,
    column,
    *,
    since_days: int | None = None,
) -> list[dict[str, Any]]:
    stmt = select(
        column.label("key"),
        func.coalesce(func.sum(TokenUsage.prompt_tokens), 0),
        func.coalesce(func.sum(TokenUsage.completion_tokens), 0),
        func.coalesce(func.sum(TokenUsage.total_tokens), 0),
        func.count(TokenUsage.id),
    ).group_by(column).order_by(func.sum(TokenUsage.total_tokens).desc())
    if since_days is not None:
        stmt = stmt.where(TokenUsage.created_at >= _since(since_days))
    rows = session.exec(stmt).all()
    return [
        {
            "key": str(r[0]),
            "prompt_tokens": int(r[1] or 0),
            "completion_tokens": int(r[2] or 0),
            "total_tokens": int(r[3] or 0),
            "calls": int(r[4] or 0),
        }
        for r in rows
        if r[0] is not None
    ]


def by_model(session: Session, *, since_days: int | None = None) -> list[dict[str, Any]]:
    return _grouped_totals(session, TokenUsage.model, since_days=since_days)


def by_feature(session: Session, *, since_days: int | None = None) -> list[dict[str, Any]]:
    return _grouped_totals(session, TokenUsage.feature, since_days=since_days)


def by_day(session: Session, *, days: int = 30) -> list[dict[str, Any]]:
    """One row per day for the last ``days`` days, oldest first.

    Days with no calls are returned with zeros so the chart has a continuous
    x-axis. We use the database's CURRENT_DATE rather than Python's now() so
    the buckets line up with the server's wall clock.
    """
    # Build a continuous series in Python; the DB might not have generate_series
    # available on SQLite.
    today = datetime.now(UTC).date()
    series = [today - timedelta(days=days - 1 - i) for i in range(days)]
    start = datetime.combine(series[0], datetime.min.time()).replace(tzinfo=UTC)
    # Pull a coarse per-day aggregation in UTC and merge.
    day_expr = func.strftime("%Y-%m-%d", TokenUsage.created_at)
    rows = session.exec(
        select(
            day_expr.label("day"),
            func.coalesce(func.sum(TokenUsage.prompt_tokens), 0),
            func.coalesce(func.sum(TokenUsage.completion_tokens), 0),
            func.coalesce(func.sum(TokenUsage.total_tokens), 0),
            func.count(TokenUsage.id),
        )
        .where(TokenUsage.created_at >= start)
        .group_by(day_expr)
        .order_by(day_expr)
    ).all()
    by_day_map: dict[str, tuple[int, int, int, int]] = {
        str(r[0]): (int(r[1] or 0), int(r[2] or 0), int(r[3] or 0), int(r[4] or 0))
        for r in rows
    }
    out: list[dict[str, Any]] = []
    for d in series:
        key = d.isoformat()
        p, c, t, n = by_day_map.get(key, (0, 0, 0, 0))
        out.append({
            "day": key,
            "prompt_tokens": p,
            "completion_tokens": c,
            "total_tokens": t,
            "calls": n,
        })
    return out


def recent(session: Session, *, limit: int = 20) -> list[dict[str, Any]]:
    """The most recent ``limit`` usage rows, newest first."""
    rows = session.exec(
        select(TokenUsage).order_by(TokenUsage.id.desc()).limit(limit)
    ).all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "model": r.model,
            "feature": r.feature,
            "job_id": r.job_id,
            "paper_id": r.paper_id,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "total_tokens": r.total_tokens,
        }
        for r in rows
    ]


def usage_for_session(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Sequence[TokenUsage]:
    """Internal helper for tests / admin views."""
    stmt = select(TokenUsage).order_by(TokenUsage.id)
    if since is not None:
        stmt = stmt.where(TokenUsage.created_at >= since)
    if until is not None:
        stmt = stmt.where(TokenUsage.created_at < until)
    return session.exec(stmt).all()


__all__ = [
    "extract_usage",
    "record_usage",
    "summary",
    "by_model",
    "by_feature",
    "by_day",
    "recent",
]
