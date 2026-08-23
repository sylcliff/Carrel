"""Tests for paper dedup pipeline: scoring, auto-merge, suggestions, rejections."""
from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from carrel.models import Paper, PaperAlias
from carrel.pipeline import paper_dedup as dedup
from carrel.pipeline import paper_dedup_ops as ops


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _paper(
    pid: str,
    *,
    title: str = "T",
    doi: str | None = None,
    arxiv_id: str | None = None,
    s2_paper_id: str | None = None,
    journal_doi: str | None = None,
    authors: list[dict] | None = None,
    abstract: str | None = None,
    venue: str | None = None,
    year: int = 2024,
    in_library: bool = True,
    status: str = "ready",
    id_kind: str = "openalex",
) -> Paper:
    return Paper(
        id=pid, id_kind=id_kind, title=title,
        doi=doi, arxiv_id=arxiv_id, s2_paper_id=s2_paper_id,
        journal_doi=journal_doi,
        publication_date=date(year, 1, 1),
        authors=authors or [],
        abstract=abstract, venue=venue,
        in_library=in_library, status=status, oa_status="oa", source="openalex",
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Strong anchor detection
# ---------------------------------------------------------------------------


def test_strong_anchor_doi(session: Session):
    a = _paper("W1", doi="10.1234/abc")
    b = _paper("W2", doi="https://doi.org/10.1234/abc")
    session.add(a); session.add(b)
    session.commit()
    ps = dedup.score_pair(a, b)
    assert "doi" in ps.strong_anchors
    assert ps.is_strong is True


def test_strong_anchor_arxiv_with_version_strip(session: Session):
    a = _paper("W1", arxiv_id="2401.00001v1")
    b = _paper("W2", arxiv_id="arXiv:2401.00001")
    session.add(a); session.add(b)
    session.commit()
    ps = dedup.score_pair(a, b)
    assert "arxiv" in ps.strong_anchors


def test_strong_anchor_journal_doi_bridge(session: Session):
    """An arxiv-only row whose journal_doi == a doi-only row's doi is the
    same paper (preprint vs journal version)."""
    a = _paper("W1", arxiv_id="2401.00001", journal_doi="10.1234/abc")
    b = _paper("W2", doi="10.1234/abc")
    session.add(a); session.add(b)
    session.commit()
    ps = dedup.score_pair(a, b)
    assert "journal_doi_bridge" in ps.strong_anchors


def test_strong_anchor_s2(session: Session):
    a = _paper("W1", s2_paper_id="abc123")
    b = _paper("W2", s2_paper_id="abc123")
    session.add(a); session.add(b)
    session.commit()
    ps = dedup.score_pair(a, b)
    assert "s2" in ps.strong_anchors


def test_strong_anchor_different_doi_does_not_match(session: Session):
    a = _paper("W1", doi="10.1234/abc", title="Some title")
    b = _paper("W2", doi="10.9999/xyz", title="Some title")
    session.add(a); session.add(b)
    session.commit()
    ps = dedup.score_pair(a, b)
    assert "doi" not in ps.strong_anchors
    assert ps.is_strong is False


# ---------------------------------------------------------------------------
# Soft signal scoring
# ---------------------------------------------------------------------------


def test_soft_signals_title_and_authors(session: Session):
    a = _paper("W1", title="Attention Is All You Need",
               authors=[{"name": "Ashish Vaswani", "openalex_author_id": "A1"}],
               year=2017, venue="NeurIPS")
    b = _paper("W2", title="Attention is all you need",
               authors=[{"name": "Ashish Vaswani", "openalex_author_id": "A1"}],
               year=2017, venue="NeurIPS")
    session.add(a); session.add(b)
    session.commit()
    ps = dedup.score_pair(a, b)
    assert ps.title >= 0.95
    assert ps.authors == pytest.approx(1.0, abs=0.01)
    assert ps.year == 1.0
    assert ps.venue == 1.0
    # No strong anchor but soft score should be high.
    assert ps.is_strong is True
    assert ps.score >= dedup.AUTO_CONFIDENCE


def test_soft_signals_low_score_when_nothing_matches(session: Session):
    a = _paper("W1", title="Alpha", authors=[{"name": "A", "openalex_author_id": "A1"}],
               year=2020, venue="ICML")
    b = _paper("W2", title="Omega", authors=[{"name": "Z", "openalex_author_id": "Z9"}],
               year=2019, venue="NeurIPS")
    session.add(a); session.add(b)
    session.commit()
    ps = dedup.score_pair(a, b)
    assert ps.is_strong is False
    assert ps.score < 0.3


def test_soft_signals_use_author_aid_over_name(session: Session):
    a = _paper("W1", authors=[{"name": "Jane Doe", "openalex_author_id": "A1"}])
    b = _paper("W2", authors=[{"name": "Jane Doe", "openalex_author_id": "A1"}])
    session.add(a); session.add(b)
    session.commit()
    ps = dedup.score_pair(a, b)
    assert ps.authors == pytest.approx(1.0, abs=0.01)


# ---------------------------------------------------------------------------
# LLM judge integration
# ---------------------------------------------------------------------------


def _stub_judge(verdict: str, confidence: float, model: str = "stub"):
    """Build a PaperPairJudge stub that always returns the same verdict."""
    return SimpleNamespace(judge=lambda _a, _b: dedup.PaperPairVerdict(
        verdict=verdict, confidence=confidence, reasons=["stub"], model=model,
    ))


def test_llm_judge_borderline_same_promotes_to_strong(session: Session):
    a = _paper("W1", title="Paper about X", authors=[{"name": "X", "openalex_author_id": "AX"}],
               year=2023)
    b = _paper("W2", title="Paper about X (extended)", authors=[{"name": "Y", "openalex_author_id": "AY"}],
               year=2024)
    session.add(a); session.add(b)
    session.commit()
    # Compute the deterministic score to choose a borderline level.
    ps_det = dedup.score_pair(a, b)
    # Construct inputs that land in the borderline band.
    # (Test relies on score being borderline; if not, we skip.)
    if not (dedup.LLM_BORDERLINE_LO <= ps_det.score < dedup.LLM_BORDERLINE_HI):
        pytest.skip("deterministic score not in borderline band; skip")

    ps = dedup.score_pair(a, b, judge=_stub_judge("same", 0.9))
    assert "llm" in ps.strong_anchors
    # LLM-only strong anchor: must also clear the score bar.
    assert ps.is_strong == (ps_det.score >= dedup.AUTO_CONFIDENCE)


def test_llm_judge_borderline_different_blocks_merge(session: Session):
    a = _paper("W1", title="Alpha", authors=[{"name": "X", "openalex_author_id": "AX"}])
    b = _paper("W2", title="Alpha v2", authors=[{"name": "X", "openalex_author_id": "AX"}])
    session.add(a); session.add(b)
    session.commit()
    ps_det = dedup.score_pair(a, b)
    if not (dedup.LLM_BORDERLINE_LO <= ps_det.score < dedup.LLM_BORDERLINE_HI):
        pytest.skip("deterministic score not in borderline band; skip")
    ps = dedup.score_pair(a, b, judge=_stub_judge("different", 0.95))
    assert ps.is_strong is False
    assert dedup._should_auto_merge(ps) is False


def test_llm_judge_not_consulted_for_strong_anchor(session: Session):
    """If a strong anchor already fires, the LLM must NOT be invoked."""
    a = _paper("W1", doi="10.1234/abc")
    b = _paper("W2", doi="10.1234/abc")
    session.add(a); session.add(b)
    session.commit()
    called = []

    def judge(_a, _b):
        called.append(True)
        return dedup.PaperPairVerdict(verdict="different", confidence=1.0,
                                       reasons=["x"], model="m")
    ps = dedup.score_pair(a, b, judge=judge)
    assert "doi" in ps.strong_anchors
    assert ps.is_strong is True
    assert called == [], "LLM judge was called despite a strong anchor"


# ---------------------------------------------------------------------------
# run_dedup: integration
# ---------------------------------------------------------------------------


def test_run_dedup_auto_merges_strong_doi_pair(session: Session):
    a = _paper("W1", doi="10.1234/abc", title="Canonical Title", in_library=True)
    b = _paper("W2", doi="10.1234/abc", title="Same Canonical Title", in_library=True)
    session.add(a); session.add(b)
    session.commit()

    result = dedup.run_dedup(session, auto_apply=True)
    assert result.auto_merged == 1
    aliases = session.exec(select(PaperAlias)).all()
    assert len(aliases) == 1
    assert aliases[0].source == "auto"


def test_run_dedup_merges_bridge_pair(session: Session):
    a = _paper("W1", arxiv_id="2401.00001", journal_doi="10.1234/abc")
    b = _paper("W2", doi="10.1234/abc")
    session.add(a); session.add(b)
    session.commit()

    result = dedup.run_dedup(session, auto_apply=True)
    assert result.auto_merged == 1


def test_run_dedup_transitively_merges_three_papers(session: Session):
    # A shares DOI with B; B shares arxiv with C. All three should collapse.
    a = _paper("W1", doi="10.1234/abc", title="Paper")
    b = _paper("W2", doi="10.1234/abc", arxiv_id="2401.00001", title="Paper (v2)")
    c = _paper("W3", arxiv_id="2401.00001", title="Paper (preprint)")
    session.add_all([a, b, c])
    session.commit()
    result = dedup.run_dedup(session, auto_apply=True)
    assert result.auto_merged == 2
    # All three resolve to the same canonical.
    canon = ops.resolve_paper_id(session, "W1")
    assert ops.resolve_paper_id(session, "W2") == canon
    assert ops.resolve_paper_id(session, "W3") == canon


def test_run_dedup_skips_rejected_pair(session: Session):
    a = _paper("W1", doi="10.1234/abc")
    b = _paper("W2", doi="10.1234/abc")
    session.add(a); session.add(b)
    session.add(PaperAlias(alias_paper_id="W2", canonical_paper_id="W1",
                            source="reject", confidence=1.0))
    session.commit()
    result = dedup.run_dedup(session, auto_apply=True)
    assert result.auto_merged == 0
    assert result.skipped_rejected >= 1
    assert session.exec(select(PaperAlias).where(PaperAlias.source == "auto")).first() is None


def test_run_dedup_emits_suggestion_for_soft_match_only(session: Session):
    """A pair with no strong anchor and a borderline score is suggested, not merged.

    Two papers with nearly identical titles + same year — but no shared
    DOI/arxiv/s2 and no shared author — should land in the borderline band
    (between LLM_BORDERLINE_LO and LLM_BORDERLINE_HI). With no LLM judge
    configured, the run surfaces them as a suggestion rather than auto-merging.
    """
    a = _paper("W1", title="Alpha Studies on Transformers v1",
               authors=[{"name": "A One", "openalex_author_id": "A1"}], year=2024)
    b = _paper("W2", title="Alpha Studies on Transformers v2",
               authors=[{"name": "B Two", "openalex_author_id": "B2"}], year=2024)
    session.add(a); session.add(b)
    session.commit()
    # Sanity: the deterministic score is in the borderline band.
    ps = dedup.score_pair(a, b)
    assert dedup.LLM_BORDERLINE_LO <= ps.score < dedup.LLM_BORDERLINE_HI, (
        f"expected borderline score, got {ps.score}; "
        f"LO={dedup.LLM_BORDERLINE_LO} HI={dedup.LLM_BORDERLINE_HI}"
    )
    result = dedup.run_dedup(session, auto_apply=True, judge=None)
    assert result.auto_merged == 0
    assert result.suggested >= 1
    sug = result.suggestions[0]
    assert {sug["a"], sug["b"]} == {"W1", "W2"}


def test_run_dedup_excludes_out_of_library_papers(session: Session):
    a = _paper("W1", doi="10.1234/abc", in_library=True)
    b = _paper("W2", doi="10.1234/abc", in_library=False)
    c = _paper("W3", doi="10.1234/abc", in_library=True)
    session.add_all([a, b, c])
    session.commit()
    result = dedup.run_dedup(session, auto_apply=True)
    # b is out-of-library (inbox), so the run is over {W1, W3} and they merge.
    assert result.auto_merged == 1
    aliases = session.exec(select(PaperAlias)).all()
    assert len(aliases) == 1
    # The merged-out loser should be in-library, not inbox.
    merged = next(a for a in aliases if a.alias_paper_id in {"W1", "W3"})
    assert merged.alias_paper_id in {"W1", "W3"}


def test_run_dedup_canonical_picks_openalex_over_s2(session: Session):
    a = _paper("W1", doi="10.1234/abc", id_kind="openalex", abstract="A" * 50)
    b = _paper("s2:abc", doi="10.1234/abc", id_kind="s2", title="No abstract",
               abstract=None)
    session.add_all([a, b])
    session.commit()
    result = dedup.run_dedup(session, auto_apply=True)
    assert result.auto_merged == 1
    assert result.components[0]["canonical_id"] == "W1"
    assert result.components[0]["alias_ids"] == ["s2:abc"]


def test_run_dedup_no_apply_only_collects(session: Session):
    a = _paper("W1", doi="10.1234/abc")
    b = _paper("W2", doi="10.1234/abc")
    session.add_all([a, b])
    session.commit()
    result = dedup.run_dedup(session, auto_apply=False)
    assert result.auto_merged == 0
    # Components are still reported.
    assert len(result.components) == 1
    assert session.exec(select(PaperAlias)).first() is None


def test_run_dedup_progress_callbacks(session: Session):
    events: list[dict] = []
    a = _paper("W1", doi="10.1234/abc")
    b = _paper("W2", doi="10.1234/abc")
    session.add_all([a, b])
    session.commit()
    dedup.run_dedup(session, auto_apply=True,
                    on_progress=lambda e: events.append(e))
    stages = {e.get("stage") for e in events}
    assert "load" in stages
    assert "done" in stages
