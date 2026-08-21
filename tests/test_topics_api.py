"""API tests for /topics: trigger, counts, and ?topic= filtering on /papers."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from carrel.models import Paper, PaperStatus, PaperTopic, Topic


def _seed_paper(session, **kw) -> str:
    base = dict(
        id_kind="openalex",
        title="A Paper",
        status=PaperStatus.summarized.value,
        oa_status="oa",
        source="openalex",
        in_library=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    base.update(kw)
    pid = base["id"]
    session.add(Paper(**base))
    session.commit()
    return pid


@pytest.fixture(autouse=True)
def _stub_classify(monkeypatch):
    """Default stub: assign one topic named 'LLM Agents'."""
    def fake_topics(sess, cfg, paper_id, *, force=False, on_progress=None, **kw):
        existing = sess.query(Topic).filter(Topic.name.ilike("LLM Agents")).first()
        if existing is None:
            existing = Topic(name="LLM Agents", description="autonomous agents")
            sess.add(existing)
            sess.commit()
            sess.refresh(existing)
        if sess.get(PaperTopic, (paper_id, existing.id)) is None:
            sess.add(PaperTopic(paper_id=paper_id, topic_id=existing.id))
            sess.commit()
        if on_progress:
            on_progress({"detail": "done"})
        return sess.get(Paper, paper_id)

    monkeypatch.setattr("carrel.api.topics.topics_paper", fake_topics)


def test_trigger_single_paper_inline(client, session):
    pid = _seed_paper(session, id="W1", title="RAG paper")
    r = client.post("/topics", json={"paper_id": pid, "background": False})
    assert r.status_code == 200, r.text
    jobs = r.json()
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "topics"
    assert jobs[0]["status"] == "done"
    assert jobs[0]["stats"]["paper_id"] == pid


def test_trigger_unknown_paper_404(client):
    r = client.post("/topics", json={"paper_id": "nope"})
    assert r.status_code == 404


def test_trigger_batch(client, session):
    _seed_paper(session, id="W1", title="One")
    _seed_paper(session, id="W2", title="Two")
    r = client.post("/topics", json={"limit": 10, "background": False})
    assert r.status_code == 200, r.text
    assert len(r.json()) == 2


def test_get_topics_returns_counts(client, session):
    _seed_paper(session, id="W1")
    _seed_paper(session, id="W2")
    _seed_paper(session, id="W3", in_library=False)  # inbox, must not count

    t = Topic(name="LLM Agents")
    session.add(t)
    session.commit()
    session.refresh(t)
    session.add_all([
        PaperTopic(paper_id="W1", topic_id=t.id),  # type: ignore[arg-type]
        PaperTopic(paper_id="W2", topic_id=t.id),  # type: ignore[arg-type]
    ])
    session.commit()

    r = client.get("/topics")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "LLM Agents"
    assert rows[0]["paper_count"] == 2  # W3 is excluded


def test_list_papers_topic_filter(client, session):
    _seed_paper(session, id="W1", title="Agent paper")
    _seed_paper(session, id="W2", title="Other paper")
    t = Topic(name="LLM Agents")
    session.add(t)
    session.commit()
    session.refresh(t)
    session.add(PaperTopic(paper_id="W1", topic_id=t.id))  # type: ignore[arg-type]
    session.commit()

    r = client.get("/papers", params=[("topic", "LLM Agents")])
    assert r.status_code == 200, r.text
    ids = {p["id"] for p in r.json()}
    assert ids == {"W1"}


def test_list_papers_includes_topics_on_summary(client, session):
    _seed_paper(session, id="W1")
    t = Topic(name="LLM Agents")
    session.add(t)
    session.commit()
    session.refresh(t)
    session.add(PaperTopic(paper_id="W1", topic_id=t.id))  # type: ignore[arg-type]
    session.commit()

    r = client.get("/papers")
    body = r.json()
    paper = next(p for p in body if p["id"] == "W1")
    assert paper["topics"] == ["LLM Agents"]


def test_paper_detail_includes_topics(client, session):
    _seed_paper(session, id="W1")
    t = Topic(name="LLM Agents")
    session.add(t)
    session.commit()
    session.refresh(t)
    session.add(PaperTopic(paper_id="W1", topic_id=t.id))  # type: ignore[arg-type]
    session.commit()

    r = client.get("/papers/W1")
    assert r.status_code == 200, r.text
    assert r.json()["topics"] == ["LLM Agents"]


def test_failure_marks_job_failed(client, session, monkeypatch):
    pid = _seed_paper(session, id="W1")

    from carrel.pipeline.topics import TopicsError

    def boom(sess, cfg, paper_id, **kw):
        raise TopicsError("no LLM API key configured")

    monkeypatch.setattr("carrel.api.topics.topics_paper", boom)

    r = client.post("/topics", json={"paper_id": pid, "background": False})
    job = r.json()[0]
    assert job["kind"] == "topics"
    assert job["status"] == "failed"
    assert "API key" in job["message"]
