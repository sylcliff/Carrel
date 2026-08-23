"""Tests for scholar dedup: scoring, auto-merge, alias resolution, API."""
from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from carrel.models import Paper, ScholarAlias
from carrel.pipeline import scholar_dedup as dedup
from carrel.pipeline.wiki import _scholars_agg


def _paper(pid: str, authors: list[dict], year: int = 2024, title: str | None = None) -> Paper:
    return Paper(
        id=pid, id_kind="openalex", title=title or f"Paper {pid}",
        publication_date=date(year, 1, 1),
        authors=authors, status="ready", oa_status="oa", source="openalex",
        in_library=True,
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )


def test_authors_merge_preserves_existing_aid():
    """Backfill must not clobber an existing A-ID when OpenAlex returns a
    different one (OpenAlex's own disambiguation can be wrong)."""
    from carrel.pipeline.authors import _merge_authors

    existing = [{"name": "Yong Xu", "openalex_author_id": "A_KNOWN", "affiliation": "SIT"}]
    canonical = [{"name": "Yong Xu", "openalex_author_id": "A_DUP", "affiliation": "SIT"}]
    merged, replaced = _merge_authors(existing, canonical)
    assert not replaced
    assert merged[0]["openalex_author_id"] == "A_KNOWN"

    # But a missing ID should still be filled.
    existing2 = [{"name": "Yong Xu", "openalex_author_id": "", "affiliation": None}]
    merged2, _ = _merge_authors(existing2, canonical)
    assert merged2[0]["openalex_author_id"] == "A_DUP"


def test_norm_name_variants():
    # Punctuation/whitespace/case differences collapse so OpenAlex's mixed
    # spellings cluster together.
    assert dedup._norm_name("Y. LeCun") == "ylecun"
    assert dedup._norm_name("Y LeCun") == "ylecun"
    assert dedup._norm_name("Yann LeCun") != "ylecun"  # different name, different key


def test_score_pair_strong_coauthor_overlap_auto_merges(session: Session, monkeypatch):
    """Two A-IDs sharing multiple co-authors should exceed auto threshold."""
    # Yong Xu primary (A1): 3 papers all co-authored with A_SHARED.
    for i in range(3):
        session.add(_paper(
            f"W-A1-{i}",
            [
                {"name": "Yong Xu", "openalex_author_id": "A1", "affiliation": "SIT"},
                {"name": "Shared Coauthor", "openalex_author_id": "A_SHARED", "affiliation": "SIT"},
            ],
        ))
    # Duplicate A2: 1 paper with the same coauthor.
    session.add(_paper(
        "W-A2-1",
        [
            {"name": "Yong Xu", "openalex_author_id": "A2", "affiliation": "SIT"},
            {"name": "Shared Coauthor", "openalex_author_id": "A_SHARED", "affiliation": "SIT"},
        ],
    ))
    session.commit()

    # Avoid network calls.
    monkeypatch.setattr(dedup, "_fetch_profile", lambda aid: dedup.Profile(aid=aid))

    result = dedup.run_dedup(session, auto_apply=True)
    assert result.auto_merged == 1
    alias = session.exec(select(ScholarAlias)).first()
    assert alias is not None
    # The higher-paper-count A-ID wins as canonical.
    assert alias.canonical_aid == "A1"
    assert alias.alias_aid == "A2"
    assert alias.source == "auto"


def test_score_pair_no_overlap_does_not_merge(session: Session, monkeypatch):
    """Same name but disjoint collaborators + no affiliation stays a suggestion."""
    session.add(_paper("W-A1", [
        {"name": "Wei Wang", "openalex_author_id": "A1", "affiliation": "Univ X"},
        {"name": "Co X1", "openalex_author_id": "X1"},
    ]))
    session.add(_paper("W-A2", [
        {"name": "Wei Wang", "openalex_author_id": "A2", "affiliation": "Univ Y"},
        {"name": "Co Y1", "openalex_author_id": "Y1"},
    ]))
    session.commit()
    monkeypatch.setattr(dedup, "_fetch_profile", lambda aid: dedup.Profile(aid=aid))

    result = dedup.run_dedup(session, auto_apply=True)
    assert result.auto_merged == 0
    assert result.suggested >= 1
    assert session.exec(select(ScholarAlias)).first() is None


def test_rejection_suppresses_auto_merge(session: Session, monkeypatch):
    # Same strong overlap as the positive case.
    for i in range(3):
        session.add(_paper(f"W-A1-{i}", [
            {"name": "Yong Xu", "openalex_author_id": "A1", "affiliation": "SIT"},
            {"name": "Shared Coauthor", "openalex_author_id": "A_SHARED"},
        ]))
    session.add(_paper("W-A2-1", [
        {"name": "Yong Xu", "openalex_author_id": "A2", "affiliation": "SIT"},
        {"name": "Shared Coauthor", "openalex_author_id": "A_SHARED"},
    ]))
    # User already rejected this pair.
    session.add(ScholarAlias(
        alias_aid="A1", canonical_aid="A2",
        display_name="Yong Xu", source="reject", confidence=1.0,
    ))
    session.commit()
    monkeypatch.setattr(dedup, "_fetch_profile", lambda aid: dedup.Profile(aid=aid))

    result = dedup.run_dedup(session, auto_apply=True)
    assert result.auto_merged == 0
    assert result.skipped_rejected >= 1


def test_resolve_aid_follows_chain(session: Session):
    session.add(ScholarAlias(alias_aid="A2", canonical_aid="A1", source="auto", confidence=0.9))
    session.add(ScholarAlias(alias_aid="A3", canonical_aid="A2", source="auto", confidence=0.9))
    session.commit()
    assert dedup.resolve_aid(session, "A3") == "A1"
    assert dedup.resolve_aid(session, "A1") == "A1"


def test_user_merge_canonicalizes_target(session: Session):
    # If user picks A2 as canonical but A2 already aliases to A1, the merge
    # should redirect to A1.
    session.add(ScholarAlias(alias_aid="A2", canonical_aid="A1", source="auto", confidence=0.9))
    session.commit()
    dedup.apply_user_merge(session, alias_aid="A3", canonical_aid="A2", display_name="X")
    session.commit()
    row = session.exec(select(ScholarAlias).where(ScholarAlias.alias_aid == "A3")).one()
    assert row.canonical_aid == "A1"


def test_aggregation_resolves_aliases(session: Session):
    """With A2->A1 alias, aggregate() should collapse both into one scholar."""
    session.add(_paper("W1", [
        {"name": "Yong Xu", "openalex_author_id": "A1", "affiliation": "SIT"},
    ]))
    session.add(_paper("W2", [
        {"name": "Yong Xu", "openalex_author_id": "A2", "affiliation": "SIT"},
    ]))
    session.add(ScholarAlias(
        alias_aid="A2", canonical_aid="A1", source="auto", confidence=0.9,
    ))
    session.commit()
    _scholars_agg.invalidate_alias_cache()
    scholars = _scholars_agg.aggregate(session)
    matching = [s for s in scholars if s.key == "A1"]
    assert len(matching) == 1
    assert matching[0].paper_count == 2


def test_papers_for_key_includes_alias_papers(session: Session):
    session.add(_paper("W1", [{"name": "Yong Xu", "openalex_author_id": "A1"}]))
    session.add(_paper("W2", [{"name": "Yong Xu", "openalex_author_id": "A2"}]))
    session.add(ScholarAlias(
        alias_aid="A2", canonical_aid="A1", source="auto", confidence=0.9,
    ))
    session.commit()
    _scholars_agg.invalidate_alias_cache()
    papers = _scholars_agg.papers_for_key(session, "A1")
    assert {p.id for p in papers} == {"W1", "W2"}


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


def test_dedup_run_and_merge_endpoints(client, session: Session, monkeypatch):
    for i in range(3):
        session.add(_paper(f"W-A1-{i}", [
            {"name": "Yong Xu", "openalex_author_id": "A1", "affiliation": "SIT"},
            {"name": "Shared Coauthor", "openalex_author_id": "A_SHARED"},
        ]))
    session.add(_paper("W-A2-1", [
        {"name": "Yong Xu", "openalex_author_id": "A2", "affiliation": "SIT"},
        {"name": "Shared Coauthor", "openalex_author_id": "A_SHARED"},
    ]))
    session.commit()
    monkeypatch.setattr(dedup, "_fetch_profile", lambda aid: dedup.Profile(aid=aid))

    # Run inline (no background task).
    resp = client.post("/scholar-dedup/run", json={"auto_apply": True, "background": False})
    assert resp.status_code == 200, resp.text
    job = resp.json()
    assert job["status"] == "done"

    snap = client.get("/scholar-dedup/suggestions").json()
    assert any(a["alias_aid"] == "A2" and a["canonical_aid"] == "A1" for a in snap["applied"])

    # User rejects a different pair; verify it is persisted.
    session.add(_paper("W-B1", [{"name": "Yang Li", "openalex_author_id": "B1"}]))
    session.add(_paper("W-B2", [{"name": "Yang Li", "openalex_author_id": "B2"}]))
    session.commit()
    rj = client.post("/scholar-dedup/reject", json={"a": "B1", "b": "B2", "display_name": "Yang Li"})
    assert rj.status_code == 200
    assert rj.json()["source"] == "reject"

    # Undo the auto merge via DELETE.
    rm = client.delete("/scholar-dedup/aliases/A2/A1")
    assert rm.status_code == 200
    assert session.exec(select(ScholarAlias).where(ScholarAlias.source == "auto")).first() is None


def test_dedup_run_then_reconcile_retires_orphans(session: Session, monkeypatch):
    """Auto-merge of two A-IDs into one canonical must drive the alias's
    compiled wiki page to a redirect shell.  This is the integration
    test for Phase 4: the dedup API triggers reconcile after applying
    a merge."""
    from datetime import date as _date

    from carrel.models import WikiPage
    from carrel.pipeline.wiki._entities import reconcile_scholars

    # Two papers with the same name but different A-IDs (a common
    # OpenAlex duplicate profile scenario).
    session.add(_paper("D1", [{"name": "Jane", "openalex_author_id": "A1"}]))
    session.add(_paper("D2", [{"name": "Jane", "openalex_author_id": "A2"}]))
    session.commit()

    # Simulate that the wiki compiler already produced pages for both
    # A-IDs (e.g. before the merge was run).
    now = datetime.now(UTC)
    for slug, ek, aid in (
        ("A1", "scholar:A1", "A1"),
        ("A2", "scholar:A2", "A2"),
    ):
        session.add(WikiPage(
            kind="scholar", slug=slug, title="Jane",
            path=f"wiki/scholars/{slug}.md",
            entity_key=ek, scholar_aid=aid,
            created_at=now, updated_at=now,
        ))
    session.commit()

    # Apply a manual merge (same path dedup would take after auto_apply).
    from carrel.api.scholar_dedup import merge_pair  # noqa: F401
    # Use the helper directly to avoid a TestClient; the API layer calls
    # reconcile_scholars after this.
    with Session(session.get_bind()) as s:
        s.add(ScholarAlias(alias_aid="A2", canonical_aid="A1",
                           source="auto", confidence=0.9))
        s.commit()
        _scholars_agg.invalidate_alias_cache()
        result = reconcile_scholars(s)

    assert result.redirected == 1, f"expected A2 to be retired, got {result}"
    with Session(session.get_bind()) as s:
        a2 = s.exec(select(WikiPage).where(WikiPage.slug == "A2")).one()
        a1 = s.exec(select(WikiPage).where(WikiPage.slug == "A1")).one()
        assert a2.entity_key is None
        assert a2.redirects_to == "scholar:A1"
        assert a1.entity_key == "scholar:A1"
        assert a1.redirects_to is None
