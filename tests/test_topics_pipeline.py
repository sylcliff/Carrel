"""Tests for the LLM topic-classification pipeline."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from carrel.config import CarrelYAML
from carrel.models import Paper, PaperStatus, PaperTopic, Topic
from carrel.pipeline import topics as topic_pipe
from sqlmodel import func, select


def _make_paper(session, **kw) -> Paper:
    base = dict(
        id="W1",
        id_kind="openalex",
        title="Retrieval-Augmented Generation for LLM Agents",
        abstract="We augment large language models with retrieval and tool use.",
        status=PaperStatus.summarized.value,
        oa_status="oa",
        source="openalex",
        keywords=["rag", "agents"],
        raw_meta={"categories": ["cs.CL", "cs.AI"]},
        in_library=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    base.update(kw)
    p = Paper(**base)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _fake_llm(topics):
    def _chat(messages, **kwargs):
        return {"topics": topics}

    return _chat


@pytest.fixture(autouse=True)
def _has_key(monkeypatch):
    monkeypatch.setattr(topic_pipe.llm, "has_key_for", lambda model: True)


def _topic_count(session) -> int:
    return session.exec(select(func.count(Topic.id))).one()


def _link_count(session) -> int:
    return session.exec(select(func.count(PaperTopic.topic_id))).one()


def test_classify_creates_new_topics(session, cfg: CarrelYAML, monkeypatch):
    p = _make_paper(session)
    monkeypatch.setattr(
        topic_pipe.llm,
        "chat_json",
        _fake_llm([
            {"name": "Retrieval-Augmented Generation", "description": "RAG methods"},
            {"name": "LLM Agents", "description": "Autonomous agents"},
        ]),
    )

    topic_pipe.topics_paper(session, cfg, p.id)
    session.refresh(p)

    assert _topic_count(session) == 2
    assert _link_count(session) == 2
    names = {t.name for t in session.exec(select(Topic)).all()}
    assert names == {"Retrieval-Augmented Generation", "LLM Agents"}


def test_classify_reuses_existing_topic_case_insensitively(
    session, cfg: CarrelYAML, monkeypatch
):
    _make_paper(session, id="W1", title="First RAG paper")
    existing = Topic(name="Retrieval-Augmented Generation", description="existing")
    session.add(existing)
    session.commit()

    # LLM returns the same topic with different casing — should reuse, not duplicate.
    monkeypatch.setattr(
        topic_pipe.llm,
        "chat_json",
        _fake_llm([{"name": "retrieval-augmented generation", "description": "x"}]),
    )

    topic_pipe.topics_paper(session, cfg, "W1")

    assert _topic_count(session) == 1
    reused = session.exec(select(Topic)).one()
    # Original casing is preserved.
    assert reused.name == "Retrieval-Augmented Generation"
    assert reused.description == "existing"


def test_idempotent_skips_already_classified(session, cfg: CarrelYAML, monkeypatch):
    p = _make_paper(session)
    monkeypatch.setattr(
        topic_pipe.llm,
        "chat_json",
        _fake_llm([{"name": "LLM Agents", "description": "x"}]),
    )
    topic_pipe.topics_paper(session, cfg, p.id)
    after_first = _topic_count(session)

    # Second call should NOT invoke the LLM at all.
    def _explode(*a, **k):
        raise AssertionError("LLM should not be called when topics exist")

    monkeypatch.setattr(topic_pipe.llm, "chat_json", _explode)
    topic_pipe.topics_paper(session, cfg, p.id)

    assert _topic_count(session) == after_first == 1


def test_force_reclassifies(session, cfg: CarrelYAML, monkeypatch):
    p = _make_paper(session)
    monkeypatch.setattr(
        topic_pipe.llm,
        "chat_json",
        _fake_llm([{"name": "Old Topic", "description": "x"}]),
    )
    topic_pipe.topics_paper(session, cfg, p.id)
    assert _link_count(session) == 1

    monkeypatch.setattr(
        topic_pipe.llm,
        "chat_json",
        _fake_llm([
            {"name": "New Topic A", "description": "x"},
            {"name": "New Topic B", "description": "x"},
        ]),
    )
    topic_pipe.topics_paper(session, cfg, p.id, force=True)
    session.refresh(p)

    names = {t.name for t in session.exec(select(Topic)).all()}
    assert "Old Topic" in names  # topic row stays (could be used by others)
    assert {"New Topic A", "New Topic B"}.issubset(names)
    # Old link replaced by two new ones.
    links = session.exec(
        select(Topic.name).join(PaperTopic, PaperTopic.topic_id == Topic.id)
        .where(PaperTopic.paper_id == p.id)
    ).all()
    assert set(links) == {"New Topic A", "New Topic B"}


def test_no_key_raises(session, cfg: CarrelYAML, monkeypatch):
    p = _make_paper(session)
    monkeypatch.setattr(topic_pipe.llm, "has_key_for", lambda model: False)
    with pytest.raises(topic_pipe.TopicsError):
        topic_pipe.topics_paper(session, cfg, p.id)
    assert _link_count(session) == 0


def test_empty_topics_payload_raises(session, cfg: CarrelYAML, monkeypatch):
    p = _make_paper(session)
    monkeypatch.setattr(topic_pipe.llm, "chat_json", _fake_llm([]))
    with pytest.raises(topic_pipe.TopicsError):
        topic_pipe.topics_paper(session, cfg, p.id)


def test_select_pending_only_inlibrary_unclassified(session, cfg: CarrelYAML, monkeypatch):
    lib_classified = _make_paper(session, id="W1")
    lib_pending = _make_paper(session, id="W2", title="Unclassified paper")
    _make_paper(session, id="W3", in_library=False)  # inbox, excluded

    t = Topic(name="LLM Agents")
    session.add(t)
    session.commit()
    session.refresh(t)
    session.add(PaperTopic(paper_id=lib_classified.id, topic_id=t.id))  # type: ignore[arg-type]
    session.commit()

    pending = topic_pipe.select_pending_topics(session)
    ids = {p.id for p in pending}
    assert ids == {lib_pending.id}


def test_batch_counts_failures(session, cfg: CarrelYAML, monkeypatch):
    _make_paper(session, id="W1")
    _make_paper(session, id="W2", title="Another paper")

    # First call classifies W1; the second paper triggers an error from the LLM.
    calls = iter([
        {"topics": [{"name": "LLM Agents", "description": "x"}]},
        RuntimeError("boom"),
    ])

    def _chat(messages, **kwargs):
        val = next(calls)
        if isinstance(val, Exception):
            raise val
        return val

    monkeypatch.setattr(topic_pipe.llm, "chat_json", _chat)

    counts = topic_pipe.topics_pending(session, cfg, limit=10)
    assert counts["candidates"] == 2
    assert counts["classified"] == 1
    assert counts["failed"] == 1


def test_extract_source_categories_from_arxiv_and_openalex(session):
    p = Paper(
        id="W9",
        id_kind="openalex",
        title="x",
        raw_meta={
            "categories": ["cs.CL", "cs.AI"],
            "openalex": {
                "primary_topic": {"display_name": "Natural Language Processing"},
                "concepts": [{"display_name": "Machine learning"}],
            },
        },
        in_library=True,
    )
    cats = topic_pipe._extract_source_categories(p)
    assert "cs.CL" in cats
    assert "cs.AI" in cats
    assert "Natural Language Processing" in cats
    assert "Machine learning" in cats
