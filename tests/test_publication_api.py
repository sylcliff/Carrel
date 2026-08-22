"""API smoke tests for POST /papers/{id}/check-publication."""
from __future__ import annotations

from datetime import UTC, datetime

from carrel.models import Paper, PaperStatus


def _seed_paper(session, **kw) -> Paper:
    base = dict(
        id="W100",
        id_kind="arxiv",
        title="Publication Check Test",
        status=PaperStatus.parsed.value,
        oa_status="oa",
        source="arxiv",
        arxiv_id="2301.01234",
        in_library=True,
        discarded=False,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    base.update(kw)
    p = Paper(**base)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def test_check_publication_inline_runs_to_done(client, session, monkeypatch):
    p = _seed_paper(session)
    called = {}

    def fake_check_and_apply(sess, cfg, paper_id, *, force=False, on_progress=None):
        called["id"] = paper_id
        called["force"] = force
        paper = sess.get(Paper, paper_id)
        paper.journal_doi = "10.1021/acs.jctc.6c01122"
        sess.add(paper)
        sess.commit()
        if on_progress:
            on_progress({"stage": "publication", "detail": "Done."})
        return paper

    monkeypatch.setattr(
        "carrel.pipeline.publication_check.check_and_apply", fake_check_and_apply
    )

    r = client.post(
        f"/papers/{p.id}/check-publication",
        json={"background": False, "force": False},
    )
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["kind"] == "publication_check"
    assert job["status"] == "done"
    assert called["id"] == p.id
    assert called["force"] is False


def test_check_publication_404_for_unknown_paper(client):
    r = client.post(
        "/papers/does-not-exist/check-publication", json={"background": False}
    )
    assert r.status_code == 404


def test_check_publication_400_without_arxiv_id(client, session):
    p = _seed_paper(session, id="W-noarx", arxiv_id=None, id_kind="openalex")
    r = client.post(
        f"/papers/{p.id}/check-publication", json={"background": False}
    )
    assert r.status_code == 400
    assert "arXiv" in r.json()["detail"]


def test_check_publication_records_failure(client, session, monkeypatch):
    p = _seed_paper(session)

    def boom(sess, cfg, paper_id, **kw):
        from carrel.pipeline.process import ProcessError

        raise ProcessError("S2 lookup failed")

    monkeypatch.setattr("carrel.pipeline.publication_check.check_and_apply", boom)

    r = client.post(
        f"/papers/{p.id}/check-publication", json={"background": False}
    )
    assert r.status_code == 200
    job = r.json()
    assert job["status"] == "failed"
    assert "S2 lookup failed" in (job["message"] or "")
