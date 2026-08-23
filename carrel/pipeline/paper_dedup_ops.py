"""Paper-dedup operations: alias indirection, user-state migration, reject/undo.

This module is the single import surface for "merge two papers into one" and
its reverses. The pipeline (:mod:`carrel.pipeline.paper_dedup`) uses it to
auto-apply high-confidence matches; the API layer uses it for the user accept
/reject/undo endpoints; the one-shot migration script uses it to collapse
existing duplicates.

Design contract (mirror :mod:`carrel.pipeline.scholar_dedup`):

- The loser row is **kept** in ``papers`` (we never physically delete). The
  alias is an indirection layer resolved by :func:`resolve_paper_id`.
- On merge, the loser's user state (favorite, notes, tags, topics, chat
  history, chunks, wiki sources, citation lists, tldr/summary/keywords) is
  migrated to the canonical; a :class:`PaperMergeEvent` snapshots the loser's
  pre-migration state for audit.
- After migration, loser's ``status`` is set to ``PaperStatus.merged`` and
  its user-state columns are cleared so the row is effectively hidden.
- A ``reject`` alias (``source='reject'``) suppresses future auto-suggestions
  for the same pair. It is not followed by :func:`resolve_paper_id`.
- A merge is reversible by deleting the alias row (the user_state that
  already moved to the canonical is *not* put back; see ``PaperMergeEvent``
  for the snapshot).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlmodel import Session, select

from carrel.models import (
    ChatMessage,
    Chunk,
    Paper,
    PaperAlias,
    PaperMergeEvent,
    PaperStatus,
    PaperTag,
    PaperTopic,
    WikiSource,
)

logger = logging.getLogger(__name__)

# Cap to defend against pathological alias chains; one hop in practice.
_MAX_ALIAS_HOPS = 8


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_paper_id(session: Session, paper_id: str) -> str:
    """Follow the alias chain ``alias_paper_id -> canonical_paper_id`` to its root.

    A rejected alias (``source='reject'``) is **not** followed — it is treated
    as "no mapping", so the loser stays the loser. Chains are short (one hop
    in practice) but we loop defensively against future re-pointing.
    """
    if not paper_id:
        return paper_id
    seen: set[str] = set()
    current = paper_id
    for _ in range(_MAX_ALIAS_HOPS):
        if current in seen:
            return current
        seen.add(current)
        row = session.exec(
            select(PaperAlias).where(
                PaperAlias.alias_paper_id == current,
                PaperAlias.source != "reject",
            )
        ).first()
        if row is None:
            return current
        current = row.canonical_paper_id
    return current


def is_merged_away(session: Session, paper_id: str) -> bool:
    """True if ``paper_id`` has been merged into a different canonical (status flag)."""
    p = session.get(Paper, paper_id)
    return p is not None and p.status == PaperStatus.merged.value


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_USER_STATE_FIELDS = (
    "in_library",
    "discarded",
    "favorite",
    "notes_markdown",
    "tldr_en",
    "tldr_zh",
    "summary_zh",
    "keywords",
    "discovered_at",
)


def _snapshot_user_state(session: Session, paper: Paper) -> dict[str, Any]:
    """Capture the loser's user state (and counts of related rows) before migration."""
    tag_ids = [r.tag_id for r in session.exec(
        select(PaperTag).where(PaperTag.paper_id == paper.id)
    ).all()]
    topic_ids = [r.topic_id for r in session.exec(
        select(PaperTopic).where(PaperTopic.paper_id == paper.id)
    ).all()]
    snapshot: dict[str, Any] = {
        f: getattr(paper, f) for f in _USER_STATE_FIELDS
    }
    snapshot["status"] = paper.status
    snapshot["tag_ids"] = tag_ids
    snapshot["topic_ids"] = topic_ids
    snapshot["chat_message_count"] = len(session.exec(
        select(ChatMessage).where(ChatMessage.paper_id == paper.id)
    ).all())
    snapshot["chunk_count"] = len(session.exec(
        select(Chunk).where(Chunk.paper_id == paper.id)
    ).all())
    snapshot["wiki_source_count"] = len(session.exec(
        select(WikiSource).where(WikiSource.paper_id == paper.id)
    ).all())
    return snapshot


def _migrate_paper_tags(session: Session, *, loser_id: str, winner_id: str) -> int:
    """Move PaperTag rows from loser to winner, dropping any that would collide.

    Returns the number of tags actually moved.
    """
    loser_tags = session.exec(
        select(PaperTag).where(PaperTag.paper_id == loser_id)
    ).all()
    moved = 0
    for row in loser_tags:
        # If the winner already has this tag, just drop the loser row.
        existing = session.exec(
            select(PaperTag).where(
                PaperTag.paper_id == winner_id,
                PaperTag.tag_id == row.tag_id,
            )
        ).first()
        if existing is not None:
            session.delete(row)
            continue
        row.paper_id = winner_id
        session.add(row)
        moved += 1
    return moved


def _migrate_paper_topics(session: Session, *, loser_id: str, winner_id: str) -> int:
    loser_topics = session.exec(
        select(PaperTopic).where(PaperTopic.paper_id == loser_id)
    ).all()
    moved = 0
    for row in loser_topics:
        existing = session.exec(
            select(PaperTopic).where(
                PaperTopic.paper_id == winner_id,
                PaperTopic.topic_id == row.topic_id,
            )
        ).first()
        if existing is not None:
            session.delete(row)
            continue
        row.paper_id = winner_id
        session.add(row)
        moved += 1
    return moved


def _migrate_rebind(session: Session, *, loser_id: str, winner_id: str) -> dict[str, int]:
    """UPDATE all FK-bearing tables that point at the loser to point at the winner.

    Returns the number of rows updated per table.
    """
    counts: dict[str, int] = {}
    for model, col in (
        (ChatMessage, ChatMessage.paper_id),
        (Chunk, Chunk.paper_id),
        (WikiSource, WikiSource.paper_id),
    ):
        stmt = update(model).where(col == loser_id).values(**{col.key: winner_id})
        result = session.exec(stmt)  # type: ignore[arg-type]
        counts[model.__tablename__] = result.rowcount or 0
    return counts


def _merge_citation_lists(loser_items: list[dict] | None, winner_items: list[dict] | None) -> list[dict]:
    """Union two citing/references lists, deduped by (doi, arxiv_id, s2_id, normalized title).

    Mirrors :func:`carrel.pipeline.citations._merge_citing` semantics so the
    union matches the format the rest of the app already uses.
    """
    import re

    _norm = re.compile(r"[^a-z0-9]+")

    def _key(d: dict) -> tuple[str, str, str, str]:
        doi = (d.get("doi") or "").lower()
        arxiv = (d.get("arxiv_id") or "").lower()
        s2 = d.get("s2_paper_id") or ""
        title = _norm.sub("", (d.get("title") or "").lower())
        return (doi, arxiv, s2, title)

    out: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    # Winner first so its richer fields win on conflict.
    for src in (winner_items or []), (loser_items or []):
        for item in src:
            k = _key(item)
            if not any(k) or k in seen:
                continue
            seen.add(k)
            out.append(item)
    return out


def _upsert_alias(
    session: Session,
    *,
    alias_paper_id: str,
    canonical_paper_id: str,
    display_label: str | None,
    source: str,
    confidence: float,
    reasons: list[str] | None,
) -> PaperAlias:
    existing = session.exec(
        select(PaperAlias).where(
            PaperAlias.alias_paper_id == alias_paper_id,
            PaperAlias.canonical_paper_id == canonical_paper_id,
        )
    ).first()
    if existing is None:
        existing = PaperAlias(
            alias_paper_id=alias_paper_id,
            canonical_paper_id=canonical_paper_id,
        )
    existing.display_label = display_label
    existing.source = source
    existing.confidence = confidence
    existing.reasons = reasons
    session.add(existing)
    return existing


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


class PaperMergeError(ValueError):
    """Raised when a merge is rejected at validation time (self-merge, missing paper, etc.)."""


def apply_merge(
    session: Session,
    *,
    alias_paper_id: str,
    canonical_paper_id: str,
    source: str,
    confidence: float,
    reasons: list[str] | None = None,
    display_label: str | None = None,
    user_state_migrated: bool = True,
) -> PaperAlias:
    """Record an alias and migrate the loser's user state to the canonical.

    Idempotent: re-applying the same pair is a no-op on the alias row but
    re-runs the state migration (so this is safe to call after partial
    failure). Refuses to merge a paper into itself.

    The migration runs in the caller's transaction; the caller commits.
    """
    if not alias_paper_id or not canonical_paper_id:
        raise PaperMergeError("alias_paper_id and canonical_paper_id are required")
    if alias_paper_id == canonical_paper_id:
        raise PaperMergeError("cannot merge a paper into itself")

    # Resolve both ends so we never create a chain (A -> B, then A -> C).
    alias_root = resolve_paper_id(session, alias_paper_id)
    canonical_root = resolve_paper_id(session, canonical_paper_id)
    if alias_root == canonical_root:
        # Already merged; return existing row if any, otherwise just upsert.
        existing = session.exec(
            select(PaperAlias).where(
                PaperAlias.alias_paper_id == alias_paper_id,
                PaperAlias.canonical_paper_id == canonical_paper_id,
            )
        ).first()
        if existing is not None:
            return existing

    loser = session.get(Paper, alias_root)
    winner = session.get(Paper, canonical_root)
    if loser is None or winner is None:
        raise PaperMergeError(
            f"paper not found: loser={loser and loser.id!r} winner={winner and winner.id!r}"
        )

    # Drop any prior reject between this exact pair so the new alias wins.
    for r in session.exec(
        select(PaperAlias).where(PaperAlias.source == "reject")
    ).all():
        if {r.alias_paper_id, r.canonical_paper_id} == {alias_paper_id, canonical_paper_id}:
            session.delete(r)

    # Snapshot the loser's user state for the audit log before we touch it.
    snapshot = _snapshot_user_state(session, loser) if user_state_migrated else None
    session.add(PaperMergeEvent(
        alias_paper_id=alias_paper_id,
        canonical_paper_id=canonical_paper_id,
        source=source,
        confidence=confidence,
        reasons=reasons,
        user_state_snapshot=snapshot,
        user_state_migrated=user_state_migrated,
    ))

    if user_state_migrated:
        # 1. Move many-to-many associations.
        tags_moved = _migrate_paper_tags(session, loser_id=loser.id, winner_id=winner.id)
        topics_moved = _migrate_paper_topics(session, loser_id=loser.id, winner_id=winner.id)

        # 2. Rebind FKs.
        fk_counts = _migrate_rebind(session, loser_id=loser.id, winner_id=winner.id)

        # 3. Union LLM outputs (fill-missing for the strings; set-union for the list).
        if winner.favorite is False and loser.favorite is True:
            winner.favorite = True
        if loser.notes_markdown:
            if winner.notes_markdown and winner.notes_markdown != loser.notes_markdown:
                winner.notes_markdown = (
                    f"{winner.notes_markdown}\n\n---\n\n{loser.notes_markdown}"
                )
            else:
                winner.notes_markdown = loser.notes_markdown
        for field in ("tldr_en", "tldr_zh", "summary_zh"):
            if not getattr(winner, field) and getattr(loser, field):
                setattr(winner, field, getattr(loser, field))
        winner_keywords = set(winner.keywords or [])
        loser_keywords = set(loser.keywords or [])
        if loser_keywords - winner_keywords:
            winner.keywords = sorted(winner_keywords | loser_keywords)

        # 4. Merge citation lists.
        winner.citing_papers = _merge_citation_lists(
            loser.citing_papers, winner.citing_papers
        )
        winner.references = _merge_citation_lists(
            loser.references, winner.references
        )

        # 5. Library flags: OR semantics; discovered_at takes the earlier.
        if winner.in_library is False and loser.in_library is True:
            winner.in_library = True
        if winner.discarded is False and loser.discarded is True:
            winner.discarded = True
        if loser.discovered_at and (
            not winner.discovered_at or loser.discovered_at < winner.discovered_at
        ):
            winner.discovered_at = loser.discovered_at

        # 6. Clear loser's user state and flag it as merged.
        loser.in_library = False
        loser.discarded = False
        loser.favorite = False
        loser.notes_markdown = None
        loser.tldr_en = None
        loser.tldr_zh = None
        loser.summary_zh = None
        loser.keywords = None
        loser.status = PaperStatus.merged.value
        # Keep title/abstract/ids so debugging still has provenance.

        session.add(winner)
        session.add(loser)

        logger.info(
            "paper_dedup.merge: %s -> %s (source=%s, conf=%.2f, tags=%d, topics=%d, fk=%s)",
            alias_paper_id, canonical_paper_id, source, confidence,
            tags_moved, topics_moved, fk_counts,
        )

    # 7. Upsert the alias row last so migration errors don't leave a dangling alias.
    row = _upsert_alias(
        session,
        alias_paper_id=alias_paper_id,
        canonical_paper_id=canonical_paper_id,
        display_label=display_label,
        source=source,
        confidence=confidence,
        reasons=reasons,
    )
    return row


def apply_reject(
    session: Session,
    *,
    a: str,
    b: str,
    display_label: str | None = None,
    reasons: list[str] | None = None,
) -> PaperAlias:
    """Record that two papers are NOT the same (suppresses future auto-merge).

    Removes any prior auto/user/llm alias between the pair so the reject wins.
    """
    if not a or not b or a == b:
        raise PaperMergeError("two distinct paper ids required")
    for r in session.exec(
        select(PaperAlias).where(
            PaperAlias.alias_paper_id.in_([a, b]),
            PaperAlias.canonical_paper_id.in_([a, b]),
            PaperAlias.source != "reject",
        )
    ).all():
        session.delete(r)
    row = _upsert_alias(
        session,
        alias_paper_id=a,
        canonical_paper_id=b,
        display_label=display_label,
        source="reject",
        confidence=1.0,
        reasons=reasons or ["user-rejected"],
    )
    logger.info("paper_dedup.reject: %s and %s are different papers", a, b)
    return row


def undo_alias(
    session: Session,
    *,
    alias_paper_id: str,
    canonical_paper_id: str,
) -> bool:
    """Delete an alias row (best-effort undo).

    User state was migrated at merge time and is **not** put back; the
    ``PaperMergeEvent`` row written at merge time carries the pre-migration
    snapshot for offline recovery. Returns True if a row was deleted.
    """
    row = session.exec(
        select(PaperAlias).where(
            PaperAlias.alias_paper_id == alias_paper_id,
            PaperAlias.canonical_paper_id == canonical_paper_id,
        )
    ).first()
    if row is None:
        return False
    session.delete(row)
    # Also un-flag the loser's merged status so it becomes a normal paper again.
    # We do not put back user_state — see PaperMergeEvent.
    loser = session.get(Paper, alias_paper_id)
    if loser is not None and loser.status == PaperStatus.merged.value:
        loser.status = PaperStatus.ready.value
        session.add(loser)
    logger.info(
        "paper_dedup.undo: %s -> %s (user_state was migrated; not restored)",
        alias_paper_id, canonical_paper_id,
    )
    return True


def list_aliases(
    session: Session,
    *,
    source: str | None = None,
) -> list[PaperAlias]:
    """Read-only helper for the API snapshot endpoint."""
    stmt = select(PaperAlias)
    if source:
        stmt = stmt.where(PaperAlias.source == source)
    return list(session.exec(stmt).all())
