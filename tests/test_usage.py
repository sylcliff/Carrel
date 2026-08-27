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


# ---------------------------------------------------------------------------
# Prompt catalog
# ---------------------------------------------------------------------------


def test_api_usage_prompts_lists_every_feature(client):
    """``/usage/prompts`` returns the full prompt catalog. The set of
    feature names must cover every ``feature=`` value that
    :func:`usage.make_usage_callback` has ever been called with, so a
    typo in a call site surfaces as a missing row."""
    rows = client.get("/usage/prompts").json()
    assert isinstance(rows, list) and rows, "catalog must not be empty"
    features = {r["feature"] for r in rows}
    # Every feature visible in /usage/by-feature should also be catalogued.
    expected = {
        "summarize",
        "extract",
        "topics",
        "dedup_judge",
        "wiki_scholar",
        "wiki_concept",
        "wiki_question",
        "paper_chat",
        "wiki_chat",
        "wiki_enrich",
    }
    assert features == expected, features ^ expected
    # Each row has the contract shape the UI relies on.
    for r in rows:
        for key in (
            "feature", "label", "source", "system", "user_template", "notes",
            "system_default", "user_template_default",
            "overridden", "override_updated_at", "placeholders", "danger",
        ):
            assert key in r, f"{r['feature']} missing {key}"
        assert r["system"].strip(), f"{r['feature']} has empty system prompt"
        assert r["user_template"].strip(), f"{r['feature']} has empty user template"
        # `source` points at the module that owns the system prompt
        # constant, so an editor can jump from the UI to the file.
        assert (
            "._SYSTEM_PROMPT" in r["source"]
            or r["source"].endswith(":_SYSTEM_PROMPT")
            or "._SYSTEM_TEMPLATE" in r["source"]
            or r["source"].endswith(":_SYSTEM_TEMPLATE")
        )
    # Danger flags are correct.
    by_feat = {r["feature"]: r for r in rows}
    assert by_feat["paper_chat"]["danger"] is True
    assert by_feat["wiki_chat"]["danger"] is True
    assert by_feat["wiki_enrich"]["danger"] is True
    assert by_feat["summarize"]["danger"] is False


def test_api_usage_prompts_is_readonly(client):
    """The catalog endpoint is read-only — POST should be 405."""
    r = client.post("/usage/prompts")
    assert r.status_code == 405


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


# ---------------------------------------------------------------------------
# Prompt editor endpoints (M16)
# ---------------------------------------------------------------------------


def test_api_usage_prompts_get_detail(client):
    """GET /usage/prompts/{feature} returns default + effective + override state."""
    r = client.get("/usage/prompts/summarize")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feature"] == "summarize"
    assert body["system_default"].strip()
    assert body["user_template_default"].strip()
    assert body["system"] == body["system_default"]
    assert body["user_template"] == body["user_template_default"]
    assert body["overridden"] is False
    assert body["override_updated_at"] is None
    assert "body" in body["placeholders"]


def test_api_usage_prompts_get_unknown_feature_404(client):
    r = client.get("/usage/prompts/this_feature_does_not_exist")
    assert r.status_code == 404


def test_api_usage_prompts_put_round_trip(client):
    """PUT → list shows overridden → GET returns the new effective value → DELETE → defaults back."""
    # The validator runs over both system and user_template, so both must
    # reference the catalog's placeholders for a clean save.
    new_system = (
        "Sys: title={title} authors={authors} venue={venue_date} "
        "abstract={abstract} body={body}"
    )
    new_user = (
        "Title: {title}\nAuthors: {authors}\nVenue/date: {venue_date}\n"
        "Abstract: {abstract}\nBody: {body}"
    )

    # PUT
    r = client.put(
        "/usage/prompts/summarize",
        json={"system": new_system, "user_template": new_user},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feature"] == "summarize"
    assert body["override"]["system"] == new_system
    assert body["override"]["user_template"] == new_user
    assert body["override"]["updated_at"]
    assert body["warnings"] == []

    # List now reports overridden.
    rows = client.get("/usage/prompts").json()
    summarize_row = next(r for r in rows if r["feature"] == "summarize")
    assert summarize_row["overridden"] is True
    assert summarize_row["system"] == new_system
    assert summarize_row["user_template"] == new_user
    assert summarize_row["override_updated_at"]

    # Detail endpoint matches.
    detail = client.get("/usage/prompts/summarize").json()
    assert detail["overridden"] is True
    assert detail["system"] == new_system
    assert detail["user_template"] == new_user

    # DELETE — idempotent, 204.
    r = client.delete("/usage/prompts/summarize")
    assert r.status_code == 204
    r = client.delete("/usage/prompts/summarize")  # second time still 204
    assert r.status_code == 204

    # Back to defaults.
    detail = client.get("/usage/prompts/summarize").json()
    assert detail["overridden"] is False
    assert detail["system"] == detail["system_default"]


def test_api_usage_prompts_put_partial_update(client):
    """null = leave alone; '' = reset; non-empty = set."""
    # First set both.
    client.put(
        "/usage/prompts/summarize",
        json={"system": "S1", "user_template": "T1 {body}"},
    )

    # Update only system, user_template = null (missing in JSON) is left alone.
    r = client.put("/usage/prompts/summarize", json={"system": "S2"})
    assert r.status_code == 200
    detail = client.get("/usage/prompts/summarize").json()
    assert detail["system"] == "S2"  # updated
    assert detail["user_template"] == "T1 {body}"  # left alone

    # Now reset user_template with '' — system = null is left alone.
    r = client.put("/usage/prompts/summarize", json={"user_template": ""})
    assert r.status_code == 200
    detail = client.get("/usage/prompts/summarize").json()
    assert detail["system"] == "S2"  # left alone
    assert detail["user_template"] == detail["user_template_default"]  # reset

    # Reset system with ''.
    r = client.put("/usage/prompts/summarize", json={"system": ""})
    assert r.status_code == 200
    detail = client.get("/usage/prompts/summarize").json()
    assert detail["system"] == detail["system_default"]
    assert detail["user_template"] == detail["user_template_default"]


def test_api_usage_prompts_put_placeholder_warnings(client):
    """Bad placeholders surface in `warnings`, not 4xx."""
    # Unknown placeholder in user_template.
    r = client.put(
        "/usage/prompts/summarize",
        json={"user_template": "Hello {body} and {unknown_thing}"},
    )
    assert r.status_code == 200
    body = r.json()
    warnings = body["warnings"]
    assert any("unknown placeholder" in w and "unknown_thing" in w for w in warnings), warnings

    # Missing required placeholder.
    r = client.put(
        "/usage/prompts/summarize",
        json={"user_template": "Hello no placeholders here"},
    )
    assert r.status_code == 200
    warnings = r.json()["warnings"]
    # body, title, authors, venue_date, abstract are all required → all 5 missing.
    missing_msgs = [w for w in warnings if "missing placeholder" in w]
    assert len(missing_msgs) == 5, warnings

    # The save still happened — the row is now overridden despite the warnings.
    detail = client.get("/usage/prompts/summarize").json()
    assert detail["overridden"] is True


def test_api_usage_prompts_put_unknown_feature_404(client):
    r = client.put(
        "/usage/prompts/this_feature_does_not_exist",
        json={"system": "x"},
    )
    assert r.status_code == 404


def test_api_usage_prompts_delete_unknown_feature_404(client):
    r = client.delete("/usage/prompts/this_feature_does_not_exist")
    assert r.status_code == 404


def test_api_usage_prompts_put_invalidates_runtime_cache(client, session):
    """PUT must call invalidate so the next LLM call sees the new value
    without waiting for the 60s TTL."""
    from carrel import prompts_runtime
    # Warm the cache.
    assert (
        prompts_runtime.get_system("summarize", "D-SYS", session=session)
        != "OVERRIDE-FRESH"
    )
    # Edit.
    client.put(
        "/usage/prompts/summarize",
        json={"system": "OVERRIDE-FRESH"},
    )
    # Cache should be invalidated → next read sees the new value.
    assert (
        prompts_runtime.get_system("summarize", "D-SYS", session=session)
        == "OVERRIDE-FRESH"
    )
