"""Tests for paper dedup LLM judge: deterministic, LLM with cache, composite."""
from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlmodel import Session, select

from carrel.config import LLMConfig
from carrel.models import Paper, PaperDedupVerdict
from carrel.pipeline import paper_dedup_judge as judge_mod
from carrel.pipeline.paper_dedup import PaperPairVerdict


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
) -> Paper:
    return Paper(
        id=pid, id_kind="openalex", title=title,
        doi=doi, arxiv_id=arxiv_id, s2_paper_id=s2_paper_id,
        journal_doi=journal_doi,
        publication_date=date(year, 1, 1),
        authors=authors or [],
        abstract=abstract, venue=venue,
        in_library=True, status="ready", oa_status="oa", source="openalex",
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Deterministic judge
# ---------------------------------------------------------------------------


def test_deterministic_judge_same_on_doi_anchor(session: Session):
    a = _paper("W1", doi="10.1234/abc")
    b = _paper("W2", doi="10.1234/abc")
    v = judge_mod.DeterministicJudge().judge(a, b)
    assert v.verdict == "same"
    assert v.confidence == 1.0
    assert v.model == "deterministic"
    assert any("doi" in r for r in v.reasons)


def test_deterministic_judge_same_on_arxiv_anchor(session: Session):
    a = _paper("W1", arxiv_id="2401.00001")
    b = _paper("W2", arxiv_id="arXiv:2401.00001")
    v = judge_mod.DeterministicJudge().judge(a, b)
    assert v.verdict == "same"


def test_deterministic_judge_uncertain_on_no_anchor(session: Session):
    a = _paper("W1", title="Alpha", doi="10.1/a")
    b = _paper("W2", title="Omega", doi="10.1/b")
    v = judge_mod.DeterministicJudge().judge(a, b)
    assert v.verdict == "uncertain"
    assert v.confidence == 0.5


# ---------------------------------------------------------------------------
# LLM judge: cache hit / miss / budget
# ---------------------------------------------------------------------------


def _cfg() -> LLMConfig:
    return LLMConfig(
        summarize_model="m1", fallback_model="m1-fb",
        paper_dedup_judge_prompt_version=1,
    )


def test_llm_judge_caches_verdict_in_db(session: Session):
    a = _paper("W1", title="Paper One")
    b = _paper("W2", title="Paper Two")
    session.add_all([a, b])
    session.commit()

    with patch(
        "carrel.pipeline.paper_dedup_judge.chat_json",
        return_value={"verdict": "same", "confidence": 0.9, "reasons": ["r1"]},
    ) as chat:
        cfg = _cfg()
        j = judge_mod.LLMJudge(session, cfg, calls_remaining=10)
        v1 = j.judge(a, b)
    assert v1.verdict == "same"
    assert v1.confidence == 0.9
    assert chat.call_count == 1
    rows = session.exec(select(PaperDedupVerdict)).all()
    assert len(rows) == 1
    a_key, b_key = sorted(("W1", "W2"))
    assert rows[0].paper_a_id == a_key
    assert rows[0].paper_b_id == b_key
    assert rows[0].verdict == "same"
    assert rows[0].model == "m1"

    # Second call hits the cache.
    with patch(
        "carrel.pipeline.paper_dedup_judge.chat_json",
        return_value={"verdict": "same", "confidence": 0.9, "reasons": ["r1"]},
    ) as chat2:
        v2 = judge_mod.LLMJudge(session, cfg, calls_remaining=10).judge(a, b)
    assert v2.verdict == "same"
    assert chat2.call_count == 0, "cache miss should not have called chat_json"


def test_llm_judge_budget_exhaustion_returns_uncertain(session: Session):
    a = _paper("W1", title="Paper One")
    b = _paper("W2", title="Paper Two")
    session.add_all([a, b])
    session.commit()

    j = judge_mod.LLMJudge(session, _cfg(), calls_remaining=0)
    with patch("carrel.pipeline.paper_dedup_judge.chat_json") as chat:
        v = j.judge(a, b)
    assert v.verdict == "uncertain"
    assert chat.call_count == 0


def test_llm_judge_prompt_version_change_invalidates_cache(session: Session):
    a = _paper("W1", title="Paper One")
    b = _paper("W2", title="Paper Two")
    session.add_all([a, b])
    session.commit()

    cfg_v1 = LLMConfig(summarize_model="m1", paper_dedup_judge_prompt_version=1)
    cfg_v2 = LLMConfig(summarize_model="m1", paper_dedup_judge_prompt_version=2)

    with patch(
        "carrel.pipeline.paper_dedup_judge.chat_json",
        return_value={"verdict": "same", "confidence": 0.9, "reasons": ["r"]},
    ) as chat:
        judge_mod.LLMJudge(session, cfg_v1, calls_remaining=10).judge(a, b)
        judge_mod.LLMJudge(session, cfg_v2, calls_remaining=10).judge(a, b)
    # v1 cached its verdict; v2 should not see it.
    assert chat.call_count == 2


def test_llm_judge_swallows_chat_errors_as_uncertain(session: Session):
    from carrel.llm import LLMError

    a = _paper("W1")
    b = _paper("W2")
    session.add_all([a, b])
    session.commit()

    with patch(
        "carrel.pipeline.paper_dedup_judge.chat_json",
        side_effect=LLMError("api down"),
    ):
        v = judge_mod.LLMJudge(session, _cfg(), calls_remaining=10).judge(a, b)
    assert v.verdict == "uncertain"
    assert any("llm error" in r for r in v.reasons)


def test_llm_judge_normalizes_invalid_verdict_to_uncertain(session: Session):
    a = _paper("W1")
    b = _paper("W2")
    session.add_all([a, b])
    session.commit()

    with patch(
        "carrel.pipeline.paper_dedup_judge.chat_json",
        return_value={"verdict": "MAYBE", "confidence": 0.7, "reasons": []},
    ):
        v = judge_mod.LLMJudge(session, _cfg(), calls_remaining=10).judge(a, b)
    assert v.verdict == "uncertain"


# ---------------------------------------------------------------------------
# Composite judge
# ---------------------------------------------------------------------------


def test_composite_short_circuits_on_strong_anchor(session: Session):
    a = _paper("W1", doi="10.1234/abc")
    b = _paper("W2", doi="10.1234/abc")
    session.add_all([a, b])
    session.commit()

    # Even if the LLM would say "different", the strong anchor must win.
    fake_llm = SimpleNamespace(judge=lambda _a, _b: PaperPairVerdict(
        verdict="different", confidence=1.0, reasons=["x"], model="m", prompt_version=1,
    ))
    composite = judge_mod.CompositeJudge(det=judge_mod.DeterministicJudge(), llm=fake_llm)
    v = composite.judge(a, b)
    assert v.verdict == "same"


def test_composite_routes_borderline_to_llm(session: Session):
    a = _paper("W1", title="Borderline Alpha", doi="10.1/a")
    b = _paper("W2", title="Borderline Alpha", doi="10.1/b")
    session.add_all([a, b])
    session.commit()

    fake_llm = SimpleNamespace(judge=lambda _a, _b: PaperPairVerdict(
        verdict="same", confidence=0.92, reasons=["llm same"], model="m", prompt_version=1,
    ))
    composite = judge_mod.CompositeJudge(det=judge_mod.DeterministicJudge(), llm=fake_llm)
    v = composite.judge(a, b)
    assert v.verdict == "same"
    assert v.confidence == 0.92


def test_composite_falls_back_to_deterministic_when_no_llm(session: Session):
    a = _paper("W1", title="Borderline Alpha", doi="10.1/a")
    b = _paper("W2", title="Borderline Alpha", doi="10.1/b")
    session.add_all([a, b])
    session.commit()

    composite = judge_mod.CompositeJudge(det=judge_mod.DeterministicJudge(), llm=None)
    v = composite.judge(a, b)
    assert v.verdict == "uncertain"


# ---------------------------------------------------------------------------
# build_judge
# ---------------------------------------------------------------------------


def test_build_judge_returns_composite_with_budget(session: Session):
    cfg = LLMConfig(summarize_model="m1", paper_dedup_judge_max_calls_per_run=42)
    j = judge_mod.build_judge(session, cfg)
    assert isinstance(j, judge_mod.CompositeJudge)
    assert j.llm is not None
    assert j.llm.calls_remaining == 42
