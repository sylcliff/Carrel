"""Tests for token usage recording + the /usage API.

Covers:
- ``carrel.usage.extract_usage`` parsing of litellm-style responses
  (pydantic model_dump, dict, attribute access, missing/None).
- ``record_usage`` no-ops on None / zero usage.
- ``make_usage_callback`` is a closure that calls ``record_usage`` with
  bound (feature, job_id, paper_id).
- Aggregation helpers: ``summary``, ``by_model``, ``by_feature``, ``by_day``
  (continuous series incl. empty days), ``recent`` ordering.
- API smoke: ``GET /usage/summary`` etc. route shapes.
- End-to-end: ``llm.chat_json`` with a fake model emits usage rows when an
  ``on_usage`` callback is supplied.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from carrel import llm, usage
from carrel.models import TokenUsage


# ---------------------------------------------------------------------------
# extract_usage
# ---------------------------------------------------------------------------


class _FakeUsage:
    def __init__(self, d):
        self._d = d

    def model_dump(self):
        return self._d


def test_extract_usage_from_pydantic_model_dump():
    resp = SimpleNamespace(usage=_FakeUsage({
        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
    }))
    assert usage.extract_usage(resp) == {
        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
    }


def test_extract_usage_from_dict():
    resp = SimpleNamespace(usage={
        "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
    })
    assert usage.extract_usage(resp) == {
        "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
    }


def test_extract_usage_derives_total_when_only_sides_present():
    resp = SimpleNamespace(usage={
        "prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 0,
    })
    out = usage.extract_usage(resp)
    assert out is not None
    assert out["total_tokens"] == 12


def test_extract_usage_none_when_no_usage():
    assert usage.extract_usage(SimpleNamespace(usage=None)) is None
    assert usage.extract_usage(SimpleNamespace()) is None


def test_extract_usage_none_when_all_zero():
    resp = SimpleNamespace(usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    assert usage.extract_usage(resp) is None


def test_extract_usage_rejects_bool_for_token_counts():
    resp = SimpleNamespace(usage={"prompt_tokens": True, "completion_tokens": 5, "total_tokens": 0})
    out = usage.extract_usage(resp)
    assert out is not None
    assert out["prompt_tokens"] == 0
    assert out["completion_tokens"] == 5


# ---------------------------------------------------------------------------
# record_usage
# ---------------------------------------------------------------------------


def test_record_usage_no_op_on_none(session):
    usage.record_usage(session, model="x", feature="y", usage=None)
    session.commit()
    assert session.query(TokenUsage).count() == 0


def test_record_usage_writes_row(session):
    usage.record_usage(
        session,
        model="deepseek/deepseek-chat",
        feature="summarize",
        usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        paper_id="abc123",
    )
    session.commit()
    rows = session.query(TokenUsage).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.model == "deepseek/deepseek-chat"
    assert r.feature == "summarize"
    assert r.paper_id == "abc123"
    assert r.prompt_tokens == 100
    assert r.completion_tokens == 50
    assert r.total_tokens == 150


def test_make_usage_callback_uses_bound_args(session):
    cb = usage.make_usage_callback(
        session, feature="wiki_chat", job_id=7, paper_id=None,
    )
    resp = SimpleNamespace(usage={
        "prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3,
    })
    cb("deepseek/deepseek-chat", "wiki_chat", resp)
    session.commit()
    rows = session.query(TokenUsage).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.model == "deepseek/deepseek-chat"
    assert r.feature == "wiki_chat"
    assert r.job_id == 7
    assert r.total_tokens == 3


def test_make_usage_callback_swallows_db_errors(session, monkeypatch):
    """A failure in record_usage must not propagate; the LLM call wins."""

    def _boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(usage, "record_usage", _boom)
    cb = usage.make_usage_callback(session, feature="summarize")
    resp = SimpleNamespace(usage={
        "prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3,
    })
    # Must not raise.
    cb("m", "summarize", resp)


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------


def _seed(session, rows):
    for r in rows:
        session.add(TokenUsage(
            model=r["model"],
            feature=r["feature"],
            prompt_tokens=r.get("prompt_tokens", 0),
            completion_tokens=r.get("completion_tokens", 0),
            total_tokens=r.get("total_tokens", 0),
            job_id=r.get("job_id"),
            paper_id=r.get("paper_id"),
            created_at=r.get("created_at", datetime.now(UTC)),
        ))
    session.commit()


def test_summary_totals(session):
    _seed(session, [
        {"model": "m1", "feature": "summarize",
         "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        {"model": "m2", "feature": "extract",
         "prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    ])
    s = usage.summary(session)
    assert s["prompt_tokens"] == 30
    assert s["completion_tokens"] == 15
    assert s["total_tokens"] == 45
    assert s["calls"] == 2


def test_summary_with_since_days_filters(session):
    now = datetime.now(UTC)
    _seed(session, [
        {"model": "m1", "feature": "summarize", "total_tokens": 10,
         "created_at": now - timedelta(days=2)},
        {"model": "m1", "feature": "summarize", "total_tokens": 100,
         "created_at": now - timedelta(hours=1)},
    ])
    s7 = usage.summary(session, since_days=1)
    assert s7["calls"] == 1
    assert s7["total_tokens"] == 100
    s_all = usage.summary(session)
    assert s_all["calls"] == 2
    assert s_all["total_tokens"] == 110


def test_by_model_grouping(session):
    _seed(session, [
        {"model": "m1", "feature": "summarize", "total_tokens": 10,
         "prompt_tokens": 8, "completion_tokens": 2},
        {"model": "m1", "feature": "extract", "total_tokens": 20,
         "prompt_tokens": 16, "completion_tokens": 4},
        {"model": "m2", "feature": "summarize", "total_tokens": 5,
         "prompt_tokens": 4, "completion_tokens": 1},
    ])
    rows = usage.by_model(session)
    assert [r["key"] for r in rows] == ["m1", "m2"]
    assert rows[0]["total_tokens"] == 30
    assert rows[0]["calls"] == 2
    assert rows[1]["total_tokens"] == 5
    assert rows[1]["calls"] == 1


def test_by_feature_grouping(session):
    _seed(session, [
        {"model": "m1", "feature": "summarize", "total_tokens": 11},
        {"model": "m1", "feature": "summarize", "total_tokens": 5},
        {"model": "m1", "feature": "extract", "total_tokens": 7},
    ])
    rows = usage.by_feature(session)
    by_key = {r["key"]: r for r in rows}
    assert by_key["summarize"]["calls"] == 2
    assert by_key["summarize"]["total_tokens"] == 16
    assert by_key["extract"]["calls"] == 1
    assert by_key["extract"]["total_tokens"] == 7


def test_by_day_returns_continuous_series_with_zeros(session):
    """Days with no calls show 0 tokens / 0 calls so the chart is contiguous."""
    now = datetime.now(UTC)
    _seed(session, [
        {"model": "m1", "feature": "summarize", "total_tokens": 7,
         "prompt_tokens": 5, "completion_tokens": 2,
         "created_at": now - timedelta(days=2)},
    ])
    rows = usage.by_day(session, days=5)
    assert len(rows) == 5
    keys = [r["day"] for r in rows]
    assert keys == sorted(keys)
    days_with_data = [r for r in rows if r["total_tokens"] > 0]
    assert len(days_with_data) == 1
    assert days_with_data[0]["total_tokens"] == 7
    assert days_with_data[0]["calls"] == 1
    assert sum(1 for r in rows if r["total_tokens"] == 0) == 4


def test_recent_orders_newest_first(session):
    now = datetime.now(UTC)
    _seed(session, [
        {"model": "m1", "feature": "summarize", "total_tokens": 1,
         "created_at": now - timedelta(minutes=10)},
        {"model": "m1", "feature": "summarize", "total_tokens": 2,
         "created_at": now - timedelta(minutes=5)},
        {"model": "m1", "feature": "summarize", "total_tokens": 3,
         "created_at": now - timedelta(minutes=1)},
    ])
    rows = usage.recent(session, limit=2)
    assert [r["total_tokens"] for r in rows] == [3, 2]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_api_usage_summary_empty(client):
    r = client.get("/usage/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
    }


def test_api_usage_endpoints_shape(client, session):
    now = datetime.now(UTC)
    _seed(session, [
        {"model": "deepseek/deepseek-chat", "feature": "summarize",
         "prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8,
         "paper_id": "abc", "created_at": now},
    ])

    s = client.get("/usage/summary").json()
    assert s["total_tokens"] == 8 and s["calls"] == 1

    m = client.get("/usage/by-model").json()
    assert m[0]["key"] == "deepseek/deepseek-chat"
    assert m[0]["calls"] == 1

    f = client.get("/usage/by-feature").json()
    assert f[0]["key"] == "summarize"

    d = client.get("/usage/by-day?days=3").json()
    assert len(d) == 3
    today = datetime.now(UTC).date().isoformat()
    today_row = next(r for r in d if r["day"] == today)
    assert today_row["total_tokens"] == 8
    assert today_row["calls"] == 1

    r = client.get("/usage/recent?limit=5").json()
    assert len(r) == 1
    assert r[0]["paper_id"] == "abc"


def test_api_usage_since_days_param(client, session):
    """since_days=1 should exclude rows older than 24h."""
    now = datetime.now(UTC)
    _seed(session, [
        {"model": "m1", "feature": "summarize", "total_tokens": 10,
         "created_at": now - timedelta(days=10)},
        {"model": "m1", "feature": "summarize", "total_tokens": 20,
         "created_at": now - timedelta(hours=2)},
    ])
    s = client.get("/usage/summary?since_days=1").json()
    assert s["calls"] == 1
    assert s["total_tokens"] == 20


# ---------------------------------------------------------------------------
# End-to-end via llm.chat_json
# ---------------------------------------------------------------------------


def test_chat_json_invokes_on_usage(monkeypatch):
    """When ``on_usage`` is supplied, ``chat_json`` must call it with
    ``(model, feature, raw_response)`` after a successful completion."""
    from carrel import embeddings
    monkeypatch.setattr(embeddings, "_key_for", lambda m: "fake-key")
    fake_response = SimpleNamespace(usage={
        "prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33,
    })
    monkeypatch.setattr(
        llm, "_chat_with_retry",
        lambda *a, **kw: ({"ok": True}, fake_response),
    )

    captured = []

    def _on(model, feature, resp):
        captured.append((model, feature, resp))

    out = llm.chat_json(
        [{"role": "user", "content": "ping"}],
        model="openai/gpt-4o-mini",
        feature="summarize",
        on_usage=_on,
    )
    assert out == {"ok": True}
    assert len(captured) == 1
    model, feature, resp = captured[0]
    assert model == "openai/gpt-4o-mini"
    assert feature == "summarize"
    assert resp is fake_response


def test_chat_json_on_usage_optional(monkeypatch):
    """Without ``on_usage``, chat_json still works as before."""
    from carrel import embeddings
    monkeypatch.setattr(embeddings, "_key_for", lambda m: "fake-key")
    monkeypatch.setattr(
        llm, "_chat_with_retry",
        lambda *a, **kw: ({"x": 1}, object()),
    )

    out = llm.chat_json(
        [{"role": "user", "content": "x"}],
        model="openai/gpt-4o-mini",
    )
    assert out == {"x": 1}
