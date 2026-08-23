from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from carrel import main as _main
from carrel.config import CarrelYAML
from carrel.models import Paper, PaperConcept, PaperQuestion, PaperStatus
from carrel.pipeline import paper_extract as pe


# ---------------------------------------------------------------------------
# Session fixture (fresh in-memory SQLite per test)
# ---------------------------------------------------------------------------


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _engine(session: Session):
    return session.get_bind()


# ---------------------------------------------------------------------------
# Storage + paper fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cfg(tmp_path):
    """A CarrelYAML with storage rooted at ``tmp_path`` (for pipeline tests)."""
    c = CarrelYAML()
    c.storage.root = tmp_path
    (tmp_path / "papers").mkdir(parents=True, exist_ok=True)
    return c


@pytest.fixture()
def app_storage_root(client, tmp_path):
    """Override the global app_config so API tests can use a tmp storage root.

    The ``client`` fixture forces the lifespan to run first; we then mutate
    ``app_config.storage.root`` to the tmp_path so the API code reads from
    a sandboxed directory.
    """
    _main.app_config.storage.root = tmp_path
    (tmp_path / "papers").mkdir(parents=True, exist_ok=True)
    return tmp_path


_DEFAULT_MD = (
    "# Introduction\n\n"
    "We study retrieval-augmented generation for grounded answers.\n\n"
    "# Methods\n\n"
    "We compare dense passage retrieval to BM25.\n\n"
    "# Results\n\n"
    "RAG beats BM25 on factual QA.\n\n"
    "# Conclusion\n\n"
    "How can retrieval stay current with fast-moving knowledge bases?"
)


def _write_paper(
    session: Session,
    storage_root: Path,
    *,
    pid: str = "W1",
    title: str = "A study of retrieval",
    md: str | None = None,
    md_path: str = "papers/W1/paper.md",
) -> Paper:
    body = md if md is not None else _DEFAULT_MD
    p = Paper(
        id=pid,
        id_kind="openalex",
        title=title,
        abstract="Abstract: retrieval-augmented generation for grounded answers.",
        tldr_en="RAG beats BM25 on factual QA.",
        publication_date=date(2024, 1, 1),
        authors=[{"name": "Jane Doe", "openalex_author_id": "A1", "affiliation": "X"}],
        status=PaperStatus.summarized.value,
        oa_status="oa",
        source="openalex",
        in_library=True,
        md_path=md_path,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(p)
    session.commit()
    full = storage_root / md_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Mock LLM helpers
# ---------------------------------------------------------------------------


def _answer(
    *,
    concepts: list[dict] | None = None,
    questions: list[dict] | None = None,
):
    if concepts is None:
        concepts = [
            {
                "term": "Retrieval-Augmented Generation",
                "quote": "We study retrieval-augmented generation for grounded answers.",
            },
            {
                "term": "Dense Passage Retrieval",
                "quote": "We compare dense passage retrieval to BM25.",
            },
        ]
    if questions is None:
        questions = [
            {
                "question": "How can retrieval stay current with fast-moving knowledge bases?",
                "quote": "How can retrieval stay current with fast-moving knowledge bases?",
            },
        ]
    return {"concepts": concepts, "questions": questions}


def _fakes(monkeypatch, answer=None):
    monkeypatch.setattr(pe.llm, "has_key_for", lambda model: True)
    monkeypatch.setattr(pe.llm, "chat_json", lambda messages, **kw: answer or _answer())


# ---------------------------------------------------------------------------
# Pipeline tests
# ---------------------------------------------------------------------------


def test_extract_writes_concepts_and_questions(session, cfg, monkeypatch):
    _write_paper(session, cfg.storage.root)
    _fakes(monkeypatch)
    pe.extract_paper(session, cfg, "W1")
    cs = session.exec(select(PaperConcept).where(PaperConcept.paper_id == "W1")).all()
    qs = session.exec(select(PaperQuestion).where(PaperQuestion.paper_id == "W1")).all()
    assert {(c.term_normalized, c.term_display) for c in cs} == {
        ("retrieval-augmented generation", "Retrieval-Augmented Generation"),
        ("dense passage retrieval", "Dense Passage Retrieval"),
    }
    assert {q.question_normalized for q in qs} == {
        "how can retrieval stay current with fast-moving knowledge bases"
    }
    assert any(
        c.evidence_quote and "retrieval-augmented" in c.evidence_quote for c in cs
    )


def test_concept_category_parsed_and_stored(session, cfg, monkeypatch):
    # M8c: the extraction LLM now classifies each concept into one of the
    # five categories (METHOD / THEORY / DATASET / DOMAIN / PHENOMENON).
    # Verify the column is populated from the LLM's JSON output.
    _write_paper(session, cfg.storage.root)
    monkeypatch.setattr(pe.llm, "has_key_for", lambda model: True)
    monkeypatch.setattr(
        pe.llm, "chat_json",
        lambda messages, **kw: _answer(
            concepts=[
                {
                    "term": "Retrieval-Augmented Generation",
                    "category": "METHOD",
                    "quote": "We study retrieval-augmented generation for grounded answers.",
                },
                {
                    "term": "Dense Passage Retrieval",
                    "category": "METHOD",
                    "quote": "We compare dense passage retrieval to BM25.",
                },
            ],
        ),
    )
    pe.extract_paper(session, cfg, "W1")
    cs = session.exec(
        select(PaperConcept).where(PaperConcept.paper_id == "W1")
    ).all()
    by_term = {c.term_normalized: c for c in cs}
    assert by_term["retrieval-augmented generation"].category == "METHOD"
    assert by_term["dense passage retrieval"].category == "METHOD"


def test_concept_category_rejects_unknown_value(session, cfg, monkeypatch):
    # M8c: an unknown category string from the LLM (e.g. a model that hasn't
    # been updated to the new prompt yet) should be coerced to NULL rather
    # than written as-is — downstream consumers only know the five named
    # values.
    _write_paper(session, cfg.storage.root)
    monkeypatch.setattr(pe.llm, "has_key_for", lambda model: True)
    monkeypatch.setattr(
        pe.llm, "chat_json",
        lambda messages, **kw: _answer(
            concepts=[
                {
                    "term": "Retrieval-Augmented Generation",
                    "category": "ALGORITHM",
                    "quote": "We study retrieval-augmented generation for grounded answers.",
                },
            ],
        ),
    )
    pe.extract_paper(session, cfg, "W1")
    cs = session.exec(
        select(PaperConcept).where(PaperConcept.paper_id == "W1")
    ).all()
    rag = next(c for c in cs if c.term_normalized == "retrieval-augmented generation")
    assert rag.category is None


def test_idempotent_without_force(session, cfg, monkeypatch):
    _write_paper(session, cfg.storage.root)
    _fakes(monkeypatch)
    pe.extract_paper(session, cfg, "W1")
    first = session.exec(
        select(PaperConcept).where(PaperConcept.paper_id == "W1")
    ).all()
    called: list[int] = []
    monkeypatch.setattr(
        pe.llm, "chat_json",
        lambda messages, **kw: called.append(1) or _answer(),
    )
    pe.extract_paper(session, cfg, "W1")
    assert called == []
    second = session.exec(
        select(PaperConcept).where(PaperConcept.paper_id == "W1")
    ).all()
    assert len(second) == len(first)


def test_force_reruns_and_replaces(session, cfg, monkeypatch):
    _write_paper(session, cfg.storage.root)
    _fakes(monkeypatch)
    pe.extract_paper(session, cfg, "W1")
    called: list[int] = []
    monkeypatch.setattr(
        pe.llm, "chat_json",
        lambda messages, **kw: called.append(1) or _answer(
            concepts=[{
                "term": "Constitutional AI",
                "quote": "We study retrieval-augmented generation for grounded answers.",
            }],
        ),
    )
    pe.extract_paper(session, cfg, "W1", force=True)
    assert called == [1]
    cs = session.exec(
        select(PaperConcept).where(PaperConcept.paper_id == "W1")
    ).all()
    assert [c.term_normalized for c in cs] == ["constitutional ai"]


def test_quote_verification_drops_hallucinated_items(session, cfg, monkeypatch):
    _write_paper(session, cfg.storage.root)
    monkeypatch.setattr(pe.llm, "has_key_for", lambda model: True)
    monkeypatch.setattr(
        pe.llm, "chat_json",
        lambda messages, **kw: {
            "concepts": [
                {
                    "term": "Real Concept",
                    "quote": "We study retrieval-augmented generation for grounded answers.",
                },
                {
                    "term": "Hallucinated Concept",
                    "quote": "this quote is not in the paper at all",
                },
            ],
            "questions": [
                {
                    "question": "Real question",
                    "quote": "How can retrieval stay current with fast-moving knowledge bases?",
                },
                {"question": "Fake question", "quote": "totally not in the body"},
            ],
        },
    )
    pe.extract_paper(session, cfg, "W1")
    cs = session.exec(select(PaperConcept).where(PaperConcept.paper_id == "W1")).all()
    qs = session.exec(select(PaperQuestion).where(PaperQuestion.paper_id == "W1")).all()
    assert {c.term_normalized for c in cs} == {"real concept"}
    assert {q.question_normalized for q in qs} == {"real question"}


def test_no_key_raises(session, cfg, monkeypatch):
    _write_paper(session, cfg.storage.root)
    monkeypatch.setattr(pe.llm, "has_key_for", lambda model: False)
    with pytest.raises(pe.PaperExtractError, match="no LLM API key"):
        pe.extract_paper(session, cfg, "W1")


def test_missing_md_raises(session, cfg, monkeypatch):
    _write_paper(session, cfg.storage.root)
    (cfg.storage.root / "papers/W1/paper.md").unlink()
    _fakes(monkeypatch)
    with pytest.raises(pe.PaperExtractError, match="missing on disk"):
        pe.extract_paper(session, cfg, "W1")


def test_deep_pick_widens_section_window(session, cfg, monkeypatch):
    """``deep=True`` widens the head/tail slice; non-deep stops at 2/2."""
    md = "\n\n".join(f"# Section {i}\n\nBody for section {i}." for i in range(8))
    _write_paper(session, cfg.storage.root, md=md)
    captured: dict = {}

    def _fake_split(md_text: str):
        return [(f"Section {i}", f"Body for section {i}.") for i in range(8)]

    def _fake_chat(messages, **kw):
        for m in messages:
            if m["role"] == "user":
                captured["body"] = m["content"]
        return _answer()

    monkeypatch.setattr(pe.chunking, "split_by_heading", _fake_split)
    monkeypatch.setattr(pe.llm, "has_key_for", lambda model: True)
    monkeypatch.setattr(pe.llm, "chat_json", _fake_chat)
    pe.extract_paper(session, cfg, "W1", deep=True)
    deep_body = captured["body"]
    assert "Section 0" in deep_body and "Section 7" in deep_body
    assert "Section 4" in deep_body  # 5+5 reaches the middle for an 8-section paper

    captured.clear()
    pe.extract_paper(session, cfg, "W1", force=True)
    shallow_body = captured["body"]
    assert "Section 0" in shallow_body and "Section 7" in shallow_body
    assert "Section 4" not in shallow_body  # 2+2 only sees 0,1,6,7


def test_select_stale_picks_first_time_paper(session, cfg, monkeypatch):
    _write_paper(session, cfg.storage.root, pid="W1", title="One")
    _write_paper(session, cfg.storage.root, pid="W2", title="Two", md_path="papers/W2/paper.md")
    _fakes(monkeypatch)
    pe.extract_paper(session, cfg, "W1")
    stale = pe.select_stale_extract(session, limit=10)
    ids = {p.id for p in stale}
    assert "W2" in ids and "W1" not in ids


def test_extract_papers_pending_counts(session, cfg, monkeypatch):
    _write_paper(session, cfg.storage.root, pid="W1", title="One")
    _write_paper(session, cfg.storage.root, pid="W2", title="Two", md_path="papers/W2/paper.md")
    _fakes(monkeypatch)
    counts = pe.extract_papers_pending(session, cfg, limit=10)
    assert counts["candidates"] == 2
    assert counts["extracted"] == 2
    assert counts["failed"] == 0


def test_body_too_short_skips_without_llm_call(session, cfg, monkeypatch):
    _write_paper(session, cfg.storage.root, md="too short")
    called: list[int] = []
    monkeypatch.setattr(pe.llm, "has_key_for", lambda model: True)
    monkeypatch.setattr(
        pe.llm, "chat_json",
        lambda messages, **kw: called.append(1) or _answer(),
    )
    pe.extract_paper(session, cfg, "W1")
    assert called == []


# ---------------------------------------------------------------------------
# API smoke tests
# ---------------------------------------------------------------------------


def test_api_endpoint_returns_jobs(client, session, app_storage_root, monkeypatch):
    """The /papers/extract endpoint creates a Job per paper and returns them."""
    _write_paper(session, app_storage_root)
    _fakes(monkeypatch)
    r = client.post(
        "/papers/extract", json={"limit": 5, "background": False, "force": False}
    )
    assert r.status_code == 200, r.text
    jobs = r.json()
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "paper_extract"
    assert jobs[0]["status"] == "done"
    assert jobs[0]["stats"]["paper_id"] == "W1"
    rows = session.exec(
        select(PaperConcept).where(PaperConcept.paper_id == "W1")
    ).all()
    assert len(rows) == 2


def test_api_endpoint_paper_id_404(client, session, app_storage_root, monkeypatch):
    r = client.post(
        "/papers/extract",
        json={"paper_id": "W-NOT-THERE", "background": False},
    )
    assert r.status_code == 404
