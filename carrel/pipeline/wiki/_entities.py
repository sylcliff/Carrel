"""Reconcile wiki catalog to the current entity set (kind-agnostic).

Why this exists
---------------
The wiki catalog's address is ``(kind, slug)``, but a scholar's *identity* can
change over the lifetime of a paper: a name-only record acquires an OpenAlex
A-ID; an A-ID is found to be a duplicate of another and merged via
``scholar_aliases``; a Chinese author's name is romanized two different ways.
The address layer happily writes a new page; the old one is left orphaned.

The fix: every kind declares an ``enumerate_entities(session)`` that returns
the *current* canonical set (as ``EntityRef`` records), and a single
:func:`reconcile_kind` makes the catalog converge to it. The same function
works for ``scholar``, ``concept``, ``question`` — no special cases per kind.

What it does
------------
For each live page of the kind:
  1. Find its current entity by matching ``(kind, slug)`` to an
     ``EntityRef`` whose slug matches.
  2. If a different ``EntityRef`` shares the same ``entity_key`` (the
     entity moved addresses), rewrite the page to point at the new
     ``(kind, slug)`` and convert the old file on disk to a redirect shell.
  3. If no ``EntityRef`` matches the page's ``entity_key`` at all, look up
     the entity's aliases (``scholar_aliases`` for the scholar kind) to
     decide whether the page should become a redirect to a still-live
     entity, or stay orphaned for ops to inspect.
  4. Idempotent: re-running on a converged catalog is a no-op.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from carrel.models import WikiPage, WikiSource

logger = logging.getLogger(__name__)

# How many consecutive redirect hops to follow when chasing a chain. Hand-edited
# files or buggy auto-merges can create cycles; without a cap, a single reconcile
# pass would loop forever.
_MAX_REDIRECT_HOPS = 4


@dataclass(frozen=True)
class EntityRef:
    """One canonical entity as the source layer sees it.

    `entity_key` is the stable identity; `kind` + `slug` are the *current*
    address the wiki should publish.  `title` is the display name; `extra`
    is for kind-specific metadata (e.g. a scholar's OpenAlex A-ID).
    """
    entity_key: str
    kind: str
    slug: str
    title: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconcileResult:
    """Counters returned by :func:`reconcile_kind`."""
    rewritten: int = 0   # page (kind, slug) updated in place
    redirected: int = 0  # page converted to a redirect shell
    moved_sources: int = 0  # WikiSource rows reassigned to the new page
    moved_files: int = 0    # files on disk renamed/converted
    unresolved: int = 0  # no source-of-truth match; left as-is for ops
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "rewritten": self.rewritten,
            "redirected": self.redirected,
            "moved_sources": self.moved_sources,
            "moved_files": self.moved_files,
            "unresolved": self.unresolved,
            "skipped": self.skipped,
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

EnumerateFn = Callable[[Session], Iterable[EntityRef]]
AliasResolver = Callable[[Session, str], str | None]
# An alias resolver maps ``entity_key -> canonical_entity_key`` (or None if
# no merge is known).  Scholar kind uses ``scholar_aliases``; other kinds
# pass a no-op (they don't have alias tables yet).

_NOOP_RESOLVER: AliasResolver = lambda _session, _key: None


def reconcile_kind(
    session: Session,
    *,
    kind: str,
    enumerate_fn: EnumerateFn,
    resolve_alias: AliasResolver = _NOOP_RESOLVER,
) -> ReconcileResult:
    """Bring the live catalog for ``kind`` into agreement with the source.

    Parameters
    ----------
    session
        DB session.
    kind
        The ``WikiKind.value`` we're reconciling.
    enumerate_fn
        Source-of-truth: returns every entity the kind should have a page
        for.  Called once; the result is memoized.
    resolve_alias
        Optional: if a page's ``entity_key`` no longer appears in the
        enumerated set, ask this whether the key has been merged into a
        different entity.  Defaults to no-op (every orphan stays orphan).
    """
    result = ReconcileResult()
    enumerated = {er.entity_key: er for er in enumerate_fn(session)}
    rows = session.exec(
        select(WikiPage).where(
            WikiPage.kind == kind,
            WikiPage.redirects_to.is_(None),
        )
    ).all()
    now = datetime.now(UTC)
    # Index live rows by entity_key so we can detect "two rows for the same
    # entity but different slugs" within a kind.
    by_entity: dict[str, list[WikiPage]] = {}
    for r in rows:
        if r.entity_key:
            by_entity.setdefault(r.entity_key, []).append(r)

    for row in rows:
        ent = row.entity_key
        if not ent:
            # Backfill orphan — leave alone, the next backfill pass will
            # assign it a key.  (reconcile doesn't fabricate keys.)
            result.unresolved += 1
            continue

        # Case A: the entity moved addresses.
        er = enumerated.get(ent)
        if er is not None and (er.kind != row.kind or er.slug != row.slug):
            target = _find_or_open_target(session, er)
            if target.id == row.id:
                # The page is already pointing at the right slug.
                result.skipped += 1
                continue
            # Move WikiSource rows + rewrite this row.
            _moved = _retarget_page(session, row, target, now)
            result.rewritten += 1
            result.moved_sources += _moved
            if _rewrite_file_as_redirect(row, target):
                result.moved_files += 1
            continue

        if er is not None:
            # Case B: the entity exists and the page already points at it.
            result.skipped += 1
            continue

        # Case C: entity not in the current set.  Try the alias resolver.
        canonical = resolve_alias(session, ent)
        if canonical and canonical in enumerated:
            target = _find_or_open_target(session, enumerated[canonical])
            if target.id == row.id:
                result.skipped += 1
                continue
            row.entity_key = None
            row.redirects_to = canonical
            row.title = target.title
            row.summary = None
            row.confidence = 0.0
            row.evidence_count = 0
            row.compiled_at = now
            session.add(row)
            _moved = _retarget_page(session, row, target, now)
            result.redirected += 1
            result.moved_sources += _moved
            if _rewrite_file_as_redirect(row, target):
                result.moved_files += 1
            continue

        # Case D: genuinely orphaned.  Log and leave alone for ops.
        logger.warning(
            "wiki reconcile: %s page id=%s slug=%s entity_key=%s "
            "has no current source-of-truth match",
            kind, row.id, row.slug, ent,
        )
        result.unresolved += 1

    session.commit()
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_or_open_target(session: Session, er: EntityRef) -> WikiPage:
    """Return the live page for ``er``; open one if it doesn't exist yet."""
    existing = session.exec(
        select(WikiPage).where(
            WikiPage.kind == er.kind,
            WikiPage.slug == er.slug,
            WikiPage.redirects_to.is_(None),
        )
    ).first()
    if existing is not None:
        return existing
    # Open a stub row at the target address.  The next compile pass will
    # fill in the body; we only need the row to exist so we can re-point
    # WikiSource rows + write the redirect shell.
    new = WikiPage(
        kind=er.kind,
        slug=er.slug,
        title=er.title,
        path=f"wiki/{er.kind}s/{er.slug}.md",
        entity_key=er.entity_key,
        links_in_count=0,
        confidence=0.0,
        evidence_count=0,
    )
    if er.kind == "scholar" and er.extra.get("scholar_aid"):
        new.scholar_aid = er.extra["scholar_aid"]
    session.add(new)
    session.flush()  # populate new.id
    return new


def _retarget_page(
    session: Session,
    row: WikiPage,
    target: WikiPage,
    now: datetime,
) -> int:
    """Re-point a page to ``target``; return WikiSource rows moved.

    If the row itself is the loser (no separate target), we only re-point
    WikiSource rows.  Otherwise we flip the row into a redirect shell
    and move the source rows to the target.
    """
    if row.id == target.id:
        return 0
    # Move WikiSource rows from row -> target.
    sources = session.exec(
        select(WikiSource).where(WikiSource.wiki_page_id == row.id)
    ).all()
    for s in sources:
        s.wiki_page_id = target.id
        session.add(s)
    return len(sources)


def _rewrite_file_as_redirect(row: WikiPage, target: WikiPage) -> bool:
    """Convert the row's on-disk file to a redirect shell. Best-effort."""
    try:
        from urllib.parse import quote
        from carrel.pipeline.wiki._frontmatter import dump
        from carrel.db import _resolve_storage_path

        full = _resolve_storage_path(row.path)
        if not full.exists():
            return False
        meta = {"redirects_to": target.entity_key or ""}
        body = (
            f"# Redirected\n\n"
            f"This page moved to "
            f"[[{target.title}]](../{row.kind}s/{quote(target.slug)}.md).\n"
        )
        text = dump(meta, body)
        tmp = full.with_suffix(full.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(full)
        return True
    except OSError as exc:
        logger.warning(
            "wiki reconcile: could not rewrite %s as redirect shell: %s",
            row.path, exc,
        )
        return False


# ---------------------------------------------------------------------------
# Convenience: scholar alias resolver
# ---------------------------------------------------------------------------


def scholar_alias_resolver(session: Session, entity_key: str) -> str | None:
    """Resolve an orphan scholar key to its current canonical key.

    Handles two cases:
      1. ``scholar:<A-ID>``: looks up ``scholar_aliases`` (the dedup table).
         ``auto`` and ``user`` sources merge the key; ``reject`` does NOT —
         the two A-IDs are different people and the page should stay orphan.
      2. ``scholar:name:<normalized>``: a name-only page whose author has since
         acquired an A-ID.  We scan the live aggregation for an entry whose
         display-name, normalized, matches.

    Returns None if no merge is known — the page stays orphan for ops.
    """
    if entity_key.startswith("scholar:A"):
        from carrel.models import ScholarAlias
        aid = entity_key[len("scholar:"):]
        row = session.exec(
            select(ScholarAlias).where(ScholarAlias.alias_aid == aid)
        ).first()
        if row is None or row.source == "reject":
            return None
        return f"scholar:{row.canonical_aid}"
    if entity_key.startswith("scholar:name:"):
        from carrel.pipeline.wiki._scholars_agg import (
            NAME_KEY_PREFIX,
            aggregate,
        )
        from carrel.pipeline.wiki._names import normalize_name
        wanted = entity_key[len("scholar:name:"):]
        # The entity_key suffix may be in either form:
        #   - normalized ("jane doe") — backfill writes this
        #   - slugified ("jane-doe") — _derive_entity_key writes this from
        #     the page's slug.  Compare against both shapes so both paths
        #     resolve correctly.
        wanted_norm = normalize_name(wanted)
        for s in aggregate(session):
            if s.key.startswith(NAME_KEY_PREFIX):
                continue  # still name-only — no A-ID acquired
            if s.key == wanted or s.key == wanted_norm:
                continue
            # The aggregator may have changed casing/whitespace; compare
            # against the normalized form.  This is a best-effort fallback
            # for a transition the auto-merge path didn't catch.
            s_norm = normalize_name(s.name)
            if s_norm == wanted_norm or s_norm == wanted:
                return f"scholar:{s.key}"
    return None


def reconcile_scholars(session: Session) -> ReconcileResult:
    """Reconcile only the scholar kind. Thin wrapper that wires up the
    scholar enumerator + alias resolver."""
    from carrel.pipeline.wiki._scholars_agg import enumerate_entities
    return reconcile_kind(
        session,
        kind="scholar",
        enumerate_fn=enumerate_entities,
        resolve_alias=scholar_alias_resolver,
    )
