"""Tests for the /agent/runs and /agent/pipelines read endpoints (M17)."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlmodel import Session

from carrel.agent_recorder import AgentRecorder


def _seed_runs(session: Session) -> tuple[int, int, int]:
    """Create three runs (two sync, one process) and return their ids."""
    sync1 = AgentRecorder(session, pipeline_id="sync", pipeline_name="Sync", trigger="manual")
    sync1.start(context={"lookback_hours": 24}, subject="first")
    with sync1.step("fetch", label="Fetch", kind="step") as s:
        s.set_output("3 records")
    sync1.finish()

    sync2 = AgentRecorder(session, pipeline_id="sync", pipeline_name="Sync", trigger="manual")
    sync2.start(context={"lookback_hours": 24}, subject="second")
    with sync2.step("fetch", label="Fetch", kind="step") as s:
        s.set_output("0 records")
    sync2.finish()

    proc = AgentRecorder(session, pipeline_id="process", pipeline_name="Process paper", trigger="job")
    proc.start(paper_id="p1", subject="paper 1")
    with proc.step("download", label="Download", kind="step") as s:
        s.set_output("ok")
    try:
        with proc.step("parse", label="Parse", kind="step"):
            raise RuntimeError("nope")
    except RuntimeError:
        pass
    proc.finish()

    return sync1.run_id or 0, sync2.run_id or 0, proc.run_id or 0


def test_list_runs_returns_all_newest_first(client: TestClient, session: Session):
    sync1_id, sync2_id, proc_id = _seed_runs(session)
    resp = client.get("/agent/runs")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 3
    # Newest first: process (last started) before sync2 before sync1.
    assert [r["pipeline_id"] for r in rows] == ["process", "sync", "sync"]
    assert rows[0]["subject"] == "paper 1"


def test_list_runs_filter_pipeline_id(client: TestClient, session: Session):
    _seed_runs(session)
    resp = client.get("/agent/runs", params={"pipeline_id": "sync"})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    assert {r["pipeline_id"] for r in rows} == {"sync"}


def test_list_runs_filter_paper_id(client: TestClient, session: Session):
    _seed_runs(session)
    resp = client.get("/agent/runs", params={"paper_id": "p1"})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["paper_id"] == "p1"


def test_get_run_includes_steps_in_seq_order(client: TestClient, session: Session):
    sync1_id, _, _ = _seed_runs(session)
    resp = client.get(f"/agent/runs/{sync1_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == sync1_id
    assert body["pipeline_id"] == "sync"
    assert len(body["steps"]) == 1
    step = body["steps"][0]
    assert step["seq"] == 1
    assert step["node_id"] == "fetch"
    assert step["output_summary"] == "3 records"
    assert step["status"] == "success"
    assert step["duration_ms"] is not None and step["duration_ms"] >= 0


def test_get_run_404(client: TestClient):
    resp = client.get("/agent/runs/9999")
    assert resp.status_code == 404


def test_get_run_status_and_counters_for_failed(client: TestClient, session: Session):
    _, _, proc_id = _seed_runs(session)
    resp = client.get(f"/agent/runs/{proc_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["step_count"] == 2
    assert body["success_count"] == 1
    assert body["failed_count"] == 1
    failed_step = next(s for s in body["steps"] if s["status"] == "failed")
    assert "RuntimeError" in (failed_step["error"] or "")


def test_pipelines_lists_one_per_pipeline_with_counts(client: TestClient, session: Session):
    _seed_runs(session)
    resp = client.get("/agent/pipelines")
    assert resp.status_code == 200
    rows = resp.json()
    by_id = {r["pipeline_id"]: r for r in rows}
    assert set(by_id.keys()) == {"sync", "process"}
    assert by_id["sync"]["run_count"] == 2
    assert by_id["process"]["run_count"] == 1
    assert by_id["process"]["last_status"] == "failed"
    assert by_id["sync"]["last_status"] == "success"
    # last_started_at and last_run_id populated
    assert by_id["process"]["last_run_id"] is not None
    assert by_id["process"]["last_started_at"] is not None
