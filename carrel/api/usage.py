"""Token usage read-only endpoints (M13) + prompt editor endpoints (M16).

Read endpoints (M13):
  * ``/usage/summary``     — totals (prompt/completion/total/calls)
  * ``/usage/by-model``    — grouped by ``model``
  * ``/usage/by-feature``  — grouped by ``feature``
  * ``/usage/by-day``      — per-day series for the last ``days`` (default 30)
  * ``/usage/recent``      — most recent N rows
  * ``/usage/prompts``     — catalog of every LLM prompt the app issues
    (effective system + user-template + override status)

Prompt editor endpoints (M16):
  * ``GET    /usage/prompts/{feature}`` — default + effective (404 if unknown)
  * ``PUT    /usage/prompts/{feature}`` — create / update override
  * ``DELETE /usage/prompts/{feature}`` — drop override (idempotent, 204)

PUT body semantics for ``system`` / ``user_template``:
  - ``null`` (key absent or JSON null) — leave that column unchanged
  - ``""`` (empty string) — reset that column back to the default
  - any other string — set the override (with placeholder validation as warnings)

The editor's PUT / DELETE handlers call :func:`prompts_runtime.invalidate`
synchronously so the next LLM call sees the new value without waiting
for the 60s TTL.
"""
from __future__ import annotations

import string
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session

from carrel import prompts, prompts_runtime, usage
from carrel.db import get_session_dep
from carrel.models import PromptOverride

router = APIRouter(tags=["usage"])


@router.get("/usage/summary")
def get_summary(
    since_days: int | None = Query(default=None, ge=1, le=3650),
    session: Session = Depends(get_session_dep),
) -> dict:
    return usage.summary(session, since_days=since_days)


@router.get("/usage/by-model")
def get_by_model(
    since_days: int | None = Query(default=None, ge=1, le=3650),
    session: Session = Depends(get_session_dep),
) -> list[dict]:
    return usage.by_model(session, since_days=since_days)


@router.get("/usage/by-feature")
def get_by_feature(
    since_days: int | None = Query(default=None, ge=1, le=3650),
    session: Session = Depends(get_session_dep),
) -> list[dict]:
    return usage.by_feature(session, since_days=since_days)


@router.get("/usage/by-day")
def get_by_day(
    days: int = Query(default=30, ge=1, le=365),
    session: Session = Depends(get_session_dep),
) -> list[dict]:
    return usage.by_day(session, days=days)


@router.get("/usage/recent")
def get_recent(
    limit: int = Query(default=20, ge=1, le=200),
    session: Session = Depends(get_session_dep),
) -> list[dict]:
    return usage.recent(session, limit=limit)


@router.get("/usage/prompts")
def get_prompts(session: Session = Depends(get_session_dep)) -> list[dict]:
    """Catalog of every LLM prompt the app issues.

    Each row's ``system`` and ``user_template`` are the **effective**
    values (override or default), and ``overridden`` is True iff the
    user has saved an override. ``placeholders`` and ``danger`` come
    straight from the catalog and drive the editor's validation
    messages and the "affects every chat user" warning banner.
    """
    return prompts.list_prompts(session)


# ----------------- Prompt editor endpoints (M16) -----------------


class PromptOverrideUpdate(BaseModel):
    """PUT body. Each field: None = leave alone, '' = reset to default, str = set."""

    system: str | None = None
    user_template: str | None = None


def _catalog_index(session: Session) -> dict[str, dict[str, Any]]:
    """Return ``{feature: row}`` for the catalog. Built fresh per call so
    the unit tests (which sometimes rebuild the catalog) stay honest.

    Takes a session (the request's) rather than opening its own — that
    way it honors the FastAPI dependency override used by tests, which
    point ``get_session_dep`` at an in-memory engine distinct from the
    app's main engine.
    """
    return {row["feature"]: row for row in prompts.list_prompts(session)}


@router.get("/usage/prompts/{feature}")
def get_prompt_detail(
    feature: str,
    session: Session = Depends(get_session_dep),
) -> dict:
    """Return default + effective + override status for one feature.

    404 if ``feature`` is not in the catalog.
    """
    rows = _catalog_index(session)
    row = rows.get(feature)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown feature: {feature}")
    return {
        "feature": row["feature"],
        "label": row["label"],
        "source": row["source"],
        "notes": row["notes"],
        "placeholders": row["placeholders"],
        "danger": row["danger"],
        "system_default": row["system_default"],
        "user_template_default": row["user_template_default"],
        "system": row["system"],
        "user_template": row["user_template"],
        "overridden": row["overridden"],
        "override_updated_at": row["override_updated_at"],
    }


@router.put("/usage/prompts/{feature}")
def put_prompt_override(
    feature: str,
    body: PromptOverrideUpdate,
    session: Session = Depends(get_session_dep),
) -> dict:
    """Create or update an override for one feature.

    Body semantics per field:
      * ``null`` or missing — leave that column alone
      * ``""`` (empty string) — reset that column back to the default
      * any other string — set the override

    Placeholder validation runs over the new value(s) and returns any
    warnings in the response; it never blocks the save.

    Returns 404 if ``feature`` is not in the catalog.
    """
    rows = _catalog_index(session)
    row = rows.get(feature)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown feature: {feature}")

    existing = session.get(PromptOverride, feature)
    if existing is None:
        existing = PromptOverride(feature=feature)
        session.add(existing)

    warnings: list[str] = []

    if body.system is not None:
        if body.system == "":
            existing.system = None
        else:
            warnings.extend(_validate_placeholders(body.system, row["placeholders"]))
            existing.system = body.system

    if body.user_template is not None:
        if body.user_template == "":
            existing.user_template = None
        else:
            warnings.extend(_validate_placeholders(body.user_template, row["placeholders"]))
            existing.user_template = body.user_template

    existing.updated_at = datetime.now(UTC)
    session.add(existing)
    session.commit()
    session.refresh(existing)

    prompts_runtime.invalidate(feature)

    return {
        "feature": feature,
        "override": {
            "system": existing.system,
            "user_template": existing.user_template,
            "updated_at": existing.updated_at.isoformat() if existing.updated_at else None,
        },
        "warnings": warnings,
    }


@router.delete("/usage/prompts/{feature}", status_code=204)
def delete_prompt_override(
    feature: str,
    session: Session = Depends(get_session_dep),
) -> None:
    """Drop the override for ``feature`` (idempotent — 204 either way)."""
    rows = _catalog_index(session)
    if feature not in rows:
        raise HTTPException(status_code=404, detail=f"unknown feature: {feature}")

    existing = session.get(PromptOverride, feature)
    if existing is not None:
        session.delete(existing)
        session.commit()
    prompts_runtime.invalidate(feature)


def _validate_placeholders(text: str, known: list[str]) -> list[str]:
    """Lightweight ``str.format``-style placeholder check.

    Surfaces two warning categories:
      * ``unknown placeholder: {x}`` — curly-brace token in ``text`` that
        is not in the catalog's known set.
      * ``missing placeholder: {x}`` — known placeholder the catalog says
        should be present but isn't in ``text``.

    Both are informational. The user can intentionally remove a
    placeholder — we surface the warning so they don't do it by
    accident, but we don't reject.
    """
    found: set[str] = set()
    for _literal, field_name, _spec, _conv in string.Formatter().parse(text):
        if field_name is None or field_name == "":
            continue
        name = field_name.split(".")[0].split("[")[0]
        if name:
            found.add(name)
    warnings: list[str] = []
    known_set = set(known)
    for u in sorted(found - known_set):
        warnings.append(f"unknown placeholder: {{{u}}}")
    for m in sorted(known_set - found):
        warnings.append(f"missing placeholder: {{{m}}}")
    return warnings
