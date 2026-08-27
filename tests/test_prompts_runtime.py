"""Tests for :mod:`carrel.prompts_runtime` — the runtime override resolver.

Covers:
  * default-when-no-override / override-wins / partial-override semantics
  * cache TTL expiry and per-feature invalidation
  * explicit ``session=`` argument is honored (uncommitted rows visible)
  * **lockstep check**: every ``feature=`` literal passed to
    :func:`carrel.usage.make_usage_callback` must be a real entry in the
    catalog. A typo on either side silently falls through to the default,
    so this guard is the only signal that the catalog and the call sites
    have drifted.
"""
from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from carrel import prompts, prompts_runtime
from carrel.models import PromptOverride


# ---------------------------------------------------------------------------
# Resolver basics
# ---------------------------------------------------------------------------


def test_resolver_returns_default_when_no_override(session):
    s = prompts_runtime.get_system("summarize", "DEFAULT-SYS", session=session)
    t = prompts_runtime.get_user_template(
        "summarize", "DEFAULT-USER {body}", session=session
    )
    assert s == "DEFAULT-SYS"
    assert t == "DEFAULT-USER {body}"


def test_resolver_returns_override_when_present(session):
    session.add(PromptOverride(
        feature="summarize",
        system="OVERRIDE-SYS",
        user_template="OVERRIDE-USER {body}",
        updated_at=datetime.now(UTC),
    ))
    session.commit()
    prompts_runtime.invalidate("summarize")

    s = prompts_runtime.get_system("summarize", "DEFAULT-SYS", session=session)
    t = prompts_runtime.get_user_template(
        "summarize", "DEFAULT-USER {body}", session=session
    )
    assert s == "OVERRIDE-SYS"
    assert t == "OVERRIDE-USER {body}"


def test_resolver_partial_override_only_overrides_set_column(session):
    """A row that sets only ``system`` should not affect ``user_template``."""
    session.add(PromptOverride(
        feature="summarize",
        system="ONLY-SYS",
        user_template=None,
        updated_at=datetime.now(UTC),
    ))
    session.commit()
    prompts_runtime.invalidate("summarize")

    assert prompts_runtime.get_system("summarize", "D-SYS", session=session) == "ONLY-SYS"
    assert (
        prompts_runtime.get_user_template("summarize", "D-USER", session=session)
        == "D-USER"
    )


def test_resolver_isolates_features(session):
    """An override on feature A must not bleed into feature B."""
    session.add(PromptOverride(
        feature="summarize", system="S", user_template="T",
        updated_at=datetime.now(UTC),
    ))
    session.commit()
    prompts_runtime.invalidate("summarize")

    assert prompts_runtime.get_system("summarize", "D-SYS", session=session) == "S"
    assert (
        prompts_runtime.get_system("extract", "DEFAULT-EXTRACT-SYS", session=session)
        == "DEFAULT-EXTRACT-SYS"
    )


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


def test_cache_ttl_expires(monkeypatch, session):
    """Manually setting ``expires_at`` to the past forces a re-read."""
    session.add(PromptOverride(
        feature="summarize", system="FRESH", user_template=None,
        updated_at=datetime.now(UTC),
    ))
    session.commit()
    prompts_runtime.invalidate("summarize")

    # First call — populates cache.
    assert prompts_runtime.get_system("summarize", "D", session=session) == "FRESH"

    # Mutate DB underneath the cache (without invalidate).
    row = session.get(PromptOverride, "summarize")
    row.system = "STALE-EDIT"
    session.add(row)
    session.commit()

    # Cache is still warm — still returns FRESH.
    assert prompts_runtime.get_system("summarize", "D", session=session) == "FRESH"

    # Force the cached entry's expiry to the past so the next call re-reads.
    key = ("summarize", prompts_runtime._SYSTEM)
    entry = prompts_runtime._CACHE[key]
    entry.expires_at = time.monotonic() - 1.0

    # Now the next call should see the new value.
    assert prompts_runtime.get_system("summarize", "D", session=session) == "STALE-EDIT"


def test_invalidate_drops_only_targeted_feature(session):
    session.add(PromptOverride(
        feature="summarize", system="S", user_template=None,
        updated_at=datetime.now(UTC),
    ))
    session.add(PromptOverride(
        feature="extract", system="E", user_template=None,
        updated_at=datetime.now(UTC),
    ))
    session.commit()
    # Populate both caches.
    prompts_runtime.get_system("summarize", "D", session=session)
    prompts_runtime.get_system("extract", "D", session=session)
    prompts_runtime.get_user_template("summarize", "D", session=session)
    prompts_runtime.get_user_template("extract", "D", session=session)

    prompts_runtime.invalidate("summarize")

    # summarize entries gone, extract entries still cached.
    assert ("summarize", prompts_runtime._SYSTEM) not in prompts_runtime._CACHE
    assert ("summarize", prompts_runtime._USER_TEMPLATE) not in prompts_runtime._CACHE
    assert ("extract", prompts_runtime._SYSTEM) in prompts_runtime._CACHE
    assert ("extract", prompts_runtime._USER_TEMPLATE) in prompts_runtime._CACHE


def test_session_arg_sees_uncommitted_row(session):
    """If the caller passes ``session=``, an uncommitted override row
    on that session should be visible to the resolver without needing
    to commit+invalidate first. (Same session = same transaction.)"""
    session.add(PromptOverride(
        feature="summarize", system="UNCOMMITTED", user_template=None,
        updated_at=datetime.now(UTC),
    ))
    # Note: do NOT commit, do NOT invalidate. The session-scoped read
    # should still see the row.
    s = prompts_runtime.get_system("summarize", "D", session=session)
    assert s == "UNCOMMITTED"


# ---------------------------------------------------------------------------
# Lockstep: catalog <-> call sites
# ---------------------------------------------------------------------------


def _scan_make_usage_callback_features() -> set[str]:
    """Grep every ``make_usage_callback(feature="X")`` literal in carrel/."""
    import subprocess

    repo = Path(__file__).resolve().parent.parent
    out = subprocess.check_output(
        [
            "grep", "-rh", '-E', r'make_usage_callback\([^)]*feature="[a-z_]+"',
            str(repo / "carrel"),
        ],
        text=True,
    )
    return set(re.findall(r'feature="([a-z_]+)"', out))


def test_runtime_every_call_site_feature_is_catalogued(session):
    """Every ``feature=`` literal passed to ``make_usage_callback`` must
    also be a row in the catalog. Catches typos on either side: a typo in
    a call site means the runtime will silently return defaults; a typo
    in the catalog means an editor UI entry the user can't actually save
    against. Both are silent failures this test makes loud."""
    catalog_features = {row["feature"] for row in prompts.list_prompts(session)}
    call_site_features = _scan_make_usage_callback_features()
    assert call_site_features, "no make_usage_callback call sites found?"
    missing = call_site_features - catalog_features
    assert not missing, (
        f"these features are used in call sites but missing from the "
        f"catalog: {sorted(missing)}"
    )
    # The catalog may carry rows that no call site uses yet — that's fine.


# ---------------------------------------------------------------------------
# Integration: override flows through to the actual LLM call
# ---------------------------------------------------------------------------


def test_runtime_override_affects_llm_input(monkeypatch, session, tmp_path):
    """End-to-end: an override saved in the DB shows up in the messages
    array that the call site hands to ``llm.chat_json``.

    Covers summarize (the simplest call site) — every other call site
    uses the same runtime helpers, so this test's pattern generalises
    to them. The other call sites are independently covered by their
    own dedicated tests.
    """
    import tempfile
    from datetime import UTC, datetime
    from carrel import embeddings, llm
    from carrel.models import Paper, PaperStatus
    from carrel.pipeline import summarize

    monkeypatch.setattr(embeddings, "_key_for", lambda m: "fake-key")

    captured: list[list[dict]] = []

    def _fake_chat_json(messages, **kw):
        captured.append(messages)
        return {"tldr_en": "x", "tldr_zh": "x", "summary_zh": "x", "keywords": ["a"]}

    monkeypatch.setattr(llm, "chat_json", _fake_chat_json)

    # Seed a paper with a parsed markdown on disk.
    md_dir = tmp_path / "papers" / "p1"
    md_dir.mkdir(parents=True)
    rel = "papers/p1/paper.md"
    (tmp_path / rel).write_text("# Hello world\nSome body text here.", encoding="utf-8")

    # We need storage.root to point at tmp_path; build a minimal cfg shim.
    from types import SimpleNamespace
    cfg = SimpleNamespace(
        storage=SimpleNamespace(root=str(tmp_path)),
        llm=SimpleNamespace(
            summarize_model="m",
            fallback_model="m",
            temperature=0.0,
            request_timeout_seconds=30,
            max_input_chars=4000,
        ),
    )
    # The call site reads the prompt via prompts_runtime, which falls back
    # to get_app_engine() when no override is cached. Point that at the
    # same in-memory engine the test session is using.
    import carrel.db as _db
    _db.app_engine = session.get_bind()
    prompts_runtime.invalidate("summarize")
    session.add(Paper(
        id="p1",
        id_kind="openalex",
        title="Test",
        in_library=True,
        status=PaperStatus.parsed.value,
        md_path=rel,
        oa_status="oa",
        source="openalex",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    ))
    session.commit()

    # 1) Default — call site uses module constant for system.
    summarize.summarize_paper(session, cfg, "p1")
    default_messages = captured[-1]
    assert default_messages[0]["content"] == summarize._SYSTEM_PROMPT
    assert "Title: Test" in default_messages[1]["content"]

    # 2) Save an override, then re-run. The next call must use it.
    session.add(PromptOverride(
        feature="summarize",
        system="OVERRIDE-SYS-MARKER",
        user_template="OVERRIDE-USER {title}/{body}",
        updated_at=datetime.now(UTC),
    ))
    session.commit()
    prompts_runtime.invalidate("summarize")

    summarize.summarize_paper(session, cfg, "p1", force=True)
    override_messages = captured[-1]
    assert override_messages[0]["content"] == "OVERRIDE-SYS-MARKER"
    assert "OVERRIDE-USER" in override_messages[1]["content"]
