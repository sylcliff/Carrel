"""Tests for the wiki identity-decoupling layer (entity_key + redirect shells +
reconcile).

Covers:

  * name normalization collapses variants
  * backfill assigns entity_key correctly (scholar A-ID, name-- slug, fallback)
  * retire converts duplicate pages into redirect shells and moves sources
  * partial unique index blocks future duplicates but allows shells
  * reconcile retires the orphan when a name-only author acquires an A-ID
  * reconcile retires the alias's page when two A-IDs get merged
  * resolve_target follows redirect chains and caps hops
  * list_pages hides redirects by default; include_redirects=true shows them
"""
from __future__ import annotations

from datetime import date as _date
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from carrel.db import (
    backfill_wiki_identity,
    init_db,
    retire_duplicate_wiki_pages,
)
from carrel.models import Paper, ScholarAlias, WikiPage, WikiSource


@pytest.fixture(name="eng")
def eng_fixture():
    """Fresh in-memory SQLite engine with full schema (incl. partial unique
    index, backfill, retire)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    return engine


def _now() -> datetime:
    return datetime.now(UTC)


def _add_paper(
    session: Session,
    pid: str,
    authors: list[dict],
    title: str | None = None,
) -> Paper:
    p = Paper(
        id=pid,
        id_kind="openalex",
        title=title or f"Paper {pid}",
        publication_date=_date(2024, 1, 1),
        authors=authors,
        status="ready",
        oa_status="oa",
        source="openalex",
        in_library=True,
        discarded=False,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _add_page(
    session: Session,
    *,
    kind: str,
    slug: str,
    title: str,
    entity_key: str | None,
    redirects_to: str | None = None,
    scholar_aid: str | None = None,
) -> WikiPage:
    row = WikiPage(
        kind=kind,
        slug=slug,
        title=title,
        path=f"wiki/{kind}s/{slug}.md",
        entity_key=entity_key,
        redirects_to=redirects_to,
        scholar_aid=scholar_aid,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Phase 0: name normalization
# ---------------------------------------------------------------------------


def test_normalize_name_collapses_variants():
    from carrel.pipeline.wiki._names import normalize_name

    assert normalize_name("He Li") == normalize_name("He-Li")
    assert normalize_name("He Li") == normalize_name("he  li")
    assert normalize_name("He Li") == normalize_name("He.Li")
    assert normalize_name("He Li") == normalize_name("He/Li")
    # Different names do NOT collapse.
    assert normalize_name("He Li") != normalize_name("He Liu")


# ---------------------------------------------------------------------------
# Phase 1: schema + backfill + retire
# ---------------------------------------------------------------------------


def test_backfill_assigns_entity_key_for_scholar_aid(eng):
    with Session(eng) as s:
        _add_paper(s, "W1", [{"name": "Jane Doe", "openalex_author_id": "A1"}])
        _add_page(
            s,
            kind="scholar",
            slug="A1",
            title="Jane Doe",
            entity_key=None,
            scholar_aid="A1",
        )
        _add_page(
            s,
            kind="scholar",
            slug="name--jane-doe",
            title="Jane Doe",
            entity_key=None,
        )
        counts = backfill_wiki_identity(eng)
    assert counts == {"updated": 2, "skipped": 0}
    with Session(eng) as s:
        rows = s.exec(select(WikiPage).order_by(WikiPage.slug)).all()
        by_slug = {r.slug: r for r in rows}
        assert by_slug["A1"].entity_key == "scholar:A1"
        # Slug "name--jane-doe" -> base "jane-doe" (the slug form, not the
        # normalize_name form; the slug already encodes the canonical form).
        assert by_slug["name--jane-doe"].entity_key == "scholar:name:jane-doe"


def test_retire_converts_duplicate_into_redirect_shell(eng):
    """Two pages with the same entity_key (one A-ID, one name-only) must
    be deduplicated: the A-ID row wins, the name-only row becomes a
    redirect shell, and WikiSource rows move to the canonical."""
    with Session(eng) as s:
        _add_paper(s, "W1", [{"name": "Jane Doe", "openalex_author_id": "A1"}])
        # Both rows get the same entity_key — this is the scenario
        # backfill produces when an A-ID was retroactively assigned to a
        # previously name-only author.  The partial unique index would
        # reject a same-key INSERT, so insert with NULL first, then
        # UPDATE to the shared key.
        canon = _add_page(
            s,
            kind="scholar",
            slug="A1",
            title="Jane Doe",
            entity_key="scholar:A1",
            scholar_aid="A1",
        )
        loser = _add_page(
            s,
            kind="scholar",
            slug="name--jane-doe",
            title="Jane Doe",
            entity_key=None,
        )
        s.add(WikiSource(wiki_page_id=loser.id, paper_id="W1", role="context", created_at=_now()))
        s.commit()
    # Bypass the partial unique index by dropping it temporarily so the
    # duplicate key can be set.  In production the backfill pass writes
    # the same key to both rows (one was inserted before the index was
    # created); the test mirrors that.
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP INDEX uq_wiki_pages_entity_key_live")
    with Session(eng) as s:
        loser = s.exec(select(WikiPage).where(WikiPage.slug == "name--jane-doe")).one()
        loser.entity_key = "scholar:A1"
        s.add(loser)
        s.commit()
    counts = retire_duplicate_wiki_pages(eng)
    assert counts["retired"] == 1
    with Session(eng) as s:
        canon = s.exec(select(WikiPage).where(WikiPage.slug == "A1")).one()
        loser = s.exec(select(WikiPage).where(WikiPage.slug == "name--jane-doe")).one()
        assert canon.entity_key == "scholar:A1" and canon.redirects_to is None
        assert loser.entity_key is None
        assert loser.redirects_to == "scholar:A1"
        canon_src = s.exec(select(WikiSource).where(WikiSource.wiki_page_id == canon.id)).all()
        loser_src = s.exec(select(WikiSource).where(WikiSource.wiki_page_id == loser.id)).all()
        assert len(canon_src) == 1 and len(loser_src) == 0


def test_partial_unique_index_blocks_dup_but_allows_shell(eng):
    """The partial unique index must reject a second live page with the
    same entity_key, but must permit a redirect shell (entity_key=NULL)."""
    with Session(eng) as s:
        _add_paper(s, "W1", [{"name": "Jane", "openalex_author_id": "A1"}])
        _add_page(
            s,
            kind="scholar",
            slug="A1",
            title="Jane",
            entity_key="scholar:A1",
            scholar_aid="A1",
        )
        s.commit()
    # Inserting a second live page with the same key must fail.
    with pytest.raises(IntegrityError):
        with Session(eng) as s:
            s.add(
                WikiPage(
                    kind="scholar",
                    slug="A2",
                    title="Jane dup",
                    path="wiki/scholars/A2.md",
                    entity_key="scholar:A1",  # collides
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
            s.commit()
    # A redirect shell (entity_key=NULL, redirects_to=key) is allowed.
    with Session(eng) as s:
        s.add(
            WikiPage(
                kind="scholar",
                slug="A-old",
                title="Jane",
                path="wiki/scholars/A-old.md",
                entity_key=None,
                redirects_to="scholar:A1",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        s.commit()
        shell = s.exec(select(WikiPage).where(WikiPage.slug == "A-old")).one()
        assert shell.redirects_to == "scholar:A1" and shell.entity_key is None


# ---------------------------------------------------------------------------
# Phase 2: reconcile
# ---------------------------------------------------------------------------


def test_reconcile_retires_orphan_after_alias_merge(eng):
    """Two A-IDs that are aliased to one canonical: the alias's page must
    become a redirect shell to the canonical after reconcile."""
    from carrel.pipeline.wiki._entities import reconcile_scholars

    with Session(eng) as s:
        _add_paper(s, "W1", [{"name": "Jane", "openalex_author_id": "A1"}])
        _add_paper(s, "W2", [{"name": "Jane", "openalex_author_id": "A1b"}])
        s.add(
            ScholarAlias(alias_aid="A1b", canonical_aid="A1", source="auto", confidence=0.95)
        )
        s.commit()
        _add_page(
            s,
            kind="scholar",
            slug="A1",
            title="Jane",
            entity_key="scholar:A1",
            scholar_aid="A1",
        )
        _add_page(
            s,
            kind="scholar",
            slug="A1b",
            title="Jane",
            entity_key="scholar:A1b",
            scholar_aid="A1b",
        )
    with Session(eng) as s:
        result = reconcile_scholars(s)
    assert result.redirected == 1
    with Session(eng) as s:
        a1b = s.exec(select(WikiPage).where(WikiPage.slug == "A1b")).one()
        assert a1b.entity_key is None
        assert a1b.redirects_to == "scholar:A1"


def test_reconcile_retires_name_only_when_author_acquires_aid(eng):
    """A name-only page whose author later gets an A-ID must become a
    redirect shell to the new A-ID page.

    Realistic setup: a paper with no A-ID drives the initial name-only
    aggregation.  Later the paper is updated with an A-ID, so the
    aggregator now exposes only the A-ID key — the name-only page is
    orphaned, the alias resolver bridges it to the new key, and the
    page becomes a redirect shell."""
    from carrel.pipeline.wiki._entities import reconcile_scholars

    with Session(eng) as s:
        # Phase 1: only a name-only paper exists; the name-only page
        # was compiled against that aggregation.
        p1 = _add_paper(
            s, "W1", [{"name": "Jane Doe", "openalex_author_id": ""}]
        )
        _add_page(
            s,
            kind="scholar",
            slug="name--jane-doe",
            title="Jane Doe",
            # In production, the entity_key suffix is the normalize_name
            # form ("jane doe"), not the slug form ("jane-doe") — backfill
            # writes this suffix and the alias resolver compares against
            # it directly.  Set it explicitly so this test exercises the
            # realistic key shape.
            entity_key="scholar:name:jane doe",
        )
        s.commit()
    # Phase 2: the same author gets an A-ID on the existing paper.  Now
    # the aggregator only emits ``A1`` (the name-only key is gone).
    with Session(eng) as s:
        p1 = s.get(Paper, "W1")
        p1.authors = [{"name": "Jane Doe", "openalex_author_id": "A1"}]
        s.add(p1)
        s.commit()
    with Session(eng) as s:
        result = reconcile_scholars(s)
    assert result.redirected >= 1, f"expected redirect, got {result}"
    with Session(eng) as s:
        shell = s.exec(
            select(WikiPage).where(WikiPage.slug == "name--jane-doe")
        ).one()
        assert shell.entity_key is None
        assert shell.redirects_to == "scholar:A1"
        # A new canonical at the A-ID slug was opened.
        canon = s.exec(select(WikiPage).where(WikiPage.slug == "A1")).one()
        assert canon.entity_key == "scholar:A1"


# ---------------------------------------------------------------------------
# Phase 3: resolve_target
# ---------------------------------------------------------------------------


def test_resolve_target_follows_chain(eng):
    from carrel.pipeline.wiki._links import clear_resolve_cache, resolve_target

    with Session(eng) as s:
        _add_page(
            s,
            kind="scholar",
            slug="A",
            title="A",
            entity_key="scholar:A1",
            scholar_aid="A1",
        )
        _add_page(
            s,
            kind="scholar",
            slug="B",
            title="B",
            entity_key=None,
            redirects_to="scholar:A1",
        )
    with Session(eng) as s:
        clear_resolve_cache()
        r = resolve_target(s, "wiki/scholars/x.md", "../scholars/B.md")
        assert r is not None and r.slug == "A"


def test_resolve_target_caps_hops(eng, caplog):
    """A broken chain (target entity_key not in the catalog) must return
    None rather than loop forever."""
    import logging

    from carrel.pipeline.wiki._links import clear_resolve_cache, resolve_target

    with Session(eng) as s:
        _add_page(
            s,
            kind="scholar",
            slug="X",
            title="X",
            entity_key=None,
            redirects_to="scholar:does_not_exist",
        )
    with Session(eng) as s:
        clear_resolve_cache()
        with caplog.at_level(logging.WARNING, logger="carrel.pipeline.wiki._links"):
            r = resolve_target(s, "wiki/scholars/x.md", "../scholars/X.md")
    assert r is None


# ---------------------------------------------------------------------------
# Phase 4: list_pages hides redirects by default
# ---------------------------------------------------------------------------


def test_list_pages_excludes_redirects_by_default(eng):
    """The default list_pages endpoint must not return redirect shells."""
    from carrel.api.wiki import router
    from carrel.db import get_session_dep

    with Session(eng) as s:
        _add_page(
            s,
            kind="scholar",
            slug="A1",
            title="Live",
            entity_key="scholar:A1",
            scholar_aid="A1",
        )
        _add_page(
            s,
            kind="scholar",
            slug="old",
            title="Shell",
            entity_key=None,
            redirects_to="scholar:A1",
        )
        s.commit()

    app = FastAPI()
    app.include_router(router)

    def _override():
        with Session(eng) as s:
            yield s

    app.dependency_overrides[get_session_dep] = _override
    client = TestClient(app)

    default = client.get("/wiki/pages?kind=scholar").json()
    slugs = [p["slug"] for p in default]
    assert "A1" in slugs and "old" not in slugs, f"unexpected: {slugs}"

    shown = client.get("/wiki/pages?kind=scholar&include_redirects=true").json()
    shown_slugs = [p["slug"] for p in shown]
    assert "A1" in shown_slugs and "old" in shown_slugs
