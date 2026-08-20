"""API smoke tests for POST /summarize (pipeline is stubbed out)."""
from __future__ import annotations

from datetime import UTC, datetime

from carrel.models import Paper, PaperStatus


def _seed_paper(session, **kw) -> str:
    base = dict(
        id="W99",
        id_kind="openalex",
        title="Summ Test",
        status=PaperStatus.parsed.value,
        oa_status="oa",
        source="openalex",
        md_path="papers/W99/paper.md",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    base.update(kw)
    session.add(Paper(**base))
    session.commit()
    return base["id"]


def test_summarize_single_paper_inline(client, session, monkeypatch):
    pid = _seed_paper(session)
    called = {}

    def fake_summarize(sess, cfg, paper_id, **kw):
        called["id"] = paper_id
        called["force"] = kw.get("force", False)
        p = sess.get(Paper, paper_id)
        p.status = PaperStatus.summarized.value
        p.tldr_en = "en"
        p.tldr_zh = "zh"
        sess.add(p)
        sess.commit()
        return p

    monkeypatch.setattr("carrel.api.summarize.summarize_paper", fake_summarize)

    r = client.post("/summarize", json={"paper_id": pid, "background": False})
    assert r.status_code == 200, r.text
    jobs = r.json()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["kind"] == "summarize"
    assert job["status"] == "done"
    assert job["stats"]["paper_id"] == pid
    assert called["id"] == pid
    assert called["force"] is False


def test_summarize_force_single_paper(client, session, monkeypatch):
    pid = _seed_paper(session)
    called = {}

    def fake_summarize(sess, cfg, paper_id, **kw):
        called["force"] = kw.get("force", False)
        return sess.get(Paper, paper_id)

    monkeypatch.setattr("carrel.api.summarize.summarize_paper", fake_summarize)

    r = client.post("/summarize", json={"paper_id": pid, "force": True})
    assert r.status_code == 200, r.text
    assert called["force"] is True


def test_summarize_single_paper_404(client):
    r = client.post("/summarize", json={"paper_id": "does-not-exist"})
    assert r.status_code == 404


def test_summarize_batch_creates_jobs(client, session, monkeypatch):
    _seed_paper(session, id="W1", title="One")
    _seed_paper(session, id="W2", title="Two")

    def fake_summarize(sess, cfg, paper_id, **kw):
        return sess.get(Paper, paper_id)

    monkeypatch.setattr("carrel.api.summarize.summarize_paper", fake_summarize)

    r = client.post("/summarize", json={"limit": 10})
    assert r.status_code == 200, r.text
    jobs = r.json()
    assert {j["kind"] for j in jobs} == {"summarize"}
    assert len(jobs) == 2


def test_summarize_failure_marks_job_failed(client, session, monkeypatch):
    pid = _seed_paper(session)

    def boom(sess, cfg, paper_id, **kw):
        from carrel.pipeline.summarize import SummarizeError
        raise SummarizeError("no LLM API key configured")

    monkeypatch.setattr("carrel.api.summarize.summarize_paper", boom)

    r = client.post("/summarize", json={"paper_id": pid})
    assert r.status_code == 200, r.text
    job = r.json()[0]
    assert job["kind"] == "summarize"
    assert job["status"] == "failed"
    assert "API key" in job["message"]
