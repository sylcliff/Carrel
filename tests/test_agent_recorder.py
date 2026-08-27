"""Tests for the agent run / step recorder (M17)."""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from carrel.agent_recorder import (
    AgentRecorder,
    PIPELINE_CATALOG,
    agent_step,
    clear_current_recorder,
    current_recorder,
    pipeline_display_name,
    run_with_recorder,
    set_current_recorder,
)
from carrel.models import AgentRun, AgentRunStatus, AgentStep, AgentStepStatus


def test_pipeline_display_name_known_and_unknown():
    assert pipeline_display_name("sync") == "Sync (discover)"
    assert pipeline_display_name("not_in_catalog") == "not_in_catalog"


def test_pipeline_catalog_keys_present():
    expected = {
        "sync", "process", "embed", "citations", "publication_check",
        "remote_fill", "paper_dedup", "scholar_dedup", "authors_backfill",
        "scholar_works_sync", "wiki", "wiki_recompile", "paper_extract",
        "scholar_enrich", "paper_chat", "wiki_chat",
    }
    assert expected.issubset(PIPELINE_CATALOG.keys())


def test_start_creates_running_run(session: Session):
    rec = AgentRecorder(
        session, pipeline_id="sync", pipeline_name="Sync", trigger="manual"
    )
    run = rec.start(context={"k": 1}, job_id=None, subject="hello")
    assert run.id is not None
    assert run.status == AgentRunStatus.running.value
    assert run.pipeline_id == "sync"
    assert run.context == {"k": 1}
    assert run.subject == "hello"
    rows = session.exec(select(AgentRun)).all()
    assert len(rows) == 1
    assert rows[0].finished_at is None


def test_step_records_seq_and_status_transitions(session: Session):
    rec = AgentRecorder(
        session, pipeline_id="sync", pipeline_name="Sync", trigger="manual"
    )
    rec.start()
    with rec.step("fetch", label="Fetch", kind="step") as s_ctx:
        s_ctx.set_output("got 3 records")
        s_ctx.set_detail({"count": 3})
    rows = session.exec(select(AgentStep)).all()
    assert len(rows) == 1
    step = rows[0]
    assert step.seq == 1
    assert step.node_id == "fetch"
    assert step.status == AgentStepStatus.success.value
    assert step.output_summary == "got 3 records"
    assert step.detail == {"count": 3}
    assert step.finished_at is not None
    assert step.duration_ms is not None and step.duration_ms >= 0


def test_step_records_failure_when_block_raises(session: Session):
    rec = AgentRecorder(session, pipeline_id="x", pipeline_name="X")
    rec.start()
    with pytest.raises(RuntimeError):
        with rec.step("boom", label="Boom", kind="step") as s:
            s.set_input("something")
            raise RuntimeError("nope")
    rows = session.exec(select(AgentStep)).all()
    assert len(rows) == 1
    step = rows[0]
    assert step.status == AgentStepStatus.failed.value
    assert "RuntimeError" in (step.error or "")


def test_finish_aggregates_counters(session: Session):
    rec = AgentRecorder(session, pipeline_id="x", pipeline_name="X")
    rec.start()
    with rec.step("a", label="A", kind="step"):
        pass
    try:
        with rec.step("b", label="B", kind="step"):
            raise RuntimeError("bad")
    except RuntimeError:
        pass
    with rec.step("c", label="C", kind="llm", feature="llm") as s:
        s.set_tokens(model="m", prompt_tokens=10, completion_tokens=5, total_tokens=15)
    rec.finish(summary={"done": True})
    run = session.exec(select(AgentRun)).one()
    assert run.status == AgentRunStatus.failed.value
    assert run.step_count == 3
    assert run.success_count == 2
    assert run.failed_count == 1
    summary = run.summary or {}
    assert summary.get("done") is True
    assert summary.get("total_tokens") == 15


def test_finish_explicit_status_wins(session: Session):
    rec = AgentRecorder(session, pipeline_id="x", pipeline_name="X")
    rec.start()
    rec.finish(status="cancelled")
    run = session.exec(select(AgentRun)).one()
    assert run.status == "cancelled"
    assert run.finished_at is not None


def test_run_with_recorder_context_manager(session: Session):
    with run_with_recorder(
        session,
        pipeline_id="sync",
        context={"lookback_hours": 24},
        subject="unit",
    ) as rec:
        assert rec.run_id is not None
        with agent_step("a", label="A", kind="step"):
            pass
    assert current_recorder() is None
    run = session.exec(select(AgentRun)).one()
    assert run.status == AgentRunStatus.success.value
    assert run.context == {"lookback_hours": 24}
    assert session.exec(select(AgentStep)).all()


def test_run_with_recorder_marks_failed_on_exception(session: Session):
    with pytest.raises(ValueError):
        with run_with_recorder(session, pipeline_id="x", subject="oops"):
            raise ValueError("nope")
    run = session.exec(select(AgentRun)).one()
    assert run.status == AgentRunStatus.failed.value
    assert "ValueError" in (run.error or "")


def test_agent_step_outside_recorder_is_noop():
    with agent_step("any", label="Any", kind="step") as s:
        s.set_input("ignored")
        s.set_tokens(model="x", prompt_tokens=1, completion_tokens=1, total_tokens=2)


def test_truncation_limits(session: Session):
    rec = AgentRecorder(session, pipeline_id="x", pipeline_name="X")
    rec.start()
    long_text = "x" * 10_000
    with rec.step("s", label="S", kind="step") as s:
        s.set_output(long_text)
    step = session.exec(select(AgentStep)).one()
    assert step.output_summary is not None
    assert len(step.output_summary) < 10_000
    assert "truncated" in step.output_summary


def test_ambient_recorder_set_and_clear_roundtrip():
    rec = AgentRecorder(object(), pipeline_id="x", pipeline_name="X")
    token = set_current_recorder(rec)
    try:
        assert current_recorder() is rec
    finally:
        clear_current_recorder(token)
    assert current_recorder() is None


def test_summary_includes_token_totals_from_steps(session: Session):
    rec = AgentRecorder(session, pipeline_id="x", pipeline_name="X")
    rec.start()
    with rec.step("a", label="A", kind="llm") as s:
        s.set_tokens(model="deepseek", prompt_tokens=100, completion_tokens=50, total_tokens=150)
    with rec.step("b", label="B", kind="llm") as s:
        s.set_tokens(model="deepseek", prompt_tokens=20, completion_tokens=10, total_tokens=30)
    rec.finish()
    run = session.exec(select(AgentRun)).one()
    summary = run.summary or {}
    assert summary.get("total_prompt_tokens") == 120
    assert summary.get("total_completion_tokens") == 60
    assert summary.get("total_tokens") == 180
