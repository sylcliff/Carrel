"""API smoke tests for POST /process (pipeline is stubbed out)."""
from __future__ import annotations

from datetime import UTC, datetime

from carrel.models import Paper, PaperStatus


def _seed_paper(session, **kw) -> str:
    base = dict(
        id="W99",
        id_kind="openalex",
        title="Proc Test",
        status=PaperStatus.pending.value,
        oa_status="oa",
        source="openalex",
        pdf_url="https://example.org/p.pdf",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    base.update(kw)
    session.add(Paper(**base))
    session.commit()
    return base["id"]


def test_process_single_paper_inline(client, session, monkeypatch):
    pid = _seed_paper(session)
    called = {}

    def fake_process(sess, cfg, paper_id, **kw):
        called["id"] = paper_id
        p = sess.get(Paper, paper_id)
        p.status = PaperStatus.parsed.value
        p.md_path = "papers/W99/paper.md"
        sess.add(p)
        sess.commit()
        return p

    monkeypatch.setattr("carrel.api.process.process_paper", fake_process)

    r = client.post("/process", json={"paper_id": pid, "background": False})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["kind"] == "download"
    assert job["status"] == "done"
    assert called["id"] == pid


def test_process_single_paper_404(client):
    r = client.post("/process", json={"paper_id": "does-not-exist"})
    assert r.status_code == 404


def test_process_batch_inline(client, session, monkeypatch):
    _seed_paper(session, id="W1", pdf_url="https://x/1.pdf")

    def fake_batch(sess, cfg, *, limit=10, on_progress=None):
        if on_progress is not None:
            on_progress({"stage": "parse", "detail": "working on it", "index": 1, "total": 1})
        return {"candidates": 1, "parsed": 1, "failed": 0}

    monkeypatch.setattr("carrel.api.process.process_pending", fake_batch)

    r = client.post("/process", json={"limit": 5, "background": False})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["kind"] == "parse"
    assert job["status"] == "done"
    assert job["stats"]["parsed"] == 1


def test_process_single_papers_persists_progress(client, session, monkeypatch):
    from carrel.models import Job

    pid = _seed_paper(session)
    seen: dict[str, str] = {}

    def fake_process(sess, cfg, paper_id, *, on_progress=None, **kw):
        if on_progress is not None:
            on_progress({"stage": "download", "detail": "Downloading PDF…"})
            # The callback must have committed the stage to the Job row.
            job_row = sess.query(Job).order_by(Job.id.desc()).first()
            seen["mid"] = job_row.stats["detail"]
            on_progress({"stage": "parse", "detail": "MinerU is parsing…"})
        p = sess.get(Paper, paper_id)
        p.status = PaperStatus.parsed.value
        sess.add(p)
        sess.commit()
        return p

    monkeypatch.setattr("carrel.api.process.process_paper", fake_process)

    r = client.post("/process", json={"paper_id": pid, "background": False})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["status"] == "done"
    assert seen["mid"] == "Downloading PDF…"  # progress persisted mid-run
    assert job["stats"]["stage"] == "done"
    assert job["stats"]["detail"] == "Done."


def test_process_records_failure(client, session, monkeypatch):
    pid = _seed_paper(session)

    def boom(sess, cfg, paper_id, **kw):
        from carrel.pipeline.process import ProcessError

        raise ProcessError("MinerU unavailable")

    monkeypatch.setattr("carrel.api.process.process_paper", boom)

    r = client.post("/process", json={"paper_id": pid, "background": False})
    assert r.status_code == 200
    job = r.json()
    assert job["status"] == "failed"
    assert "MinerU unavailable" in job["message"]
