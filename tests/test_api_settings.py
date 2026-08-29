"""Settings API tests — GET/PATCH /settings.

Each test pins ``carrel.main.CONFIG_PATH`` and ``carrel.main.app_config`` to
a ``tmp_path`` so the real ``data/config.yaml`` is never touched. The FastAPI
``client`` fixture (from ``conftest.py``) is reused; the lifespan still
boots, but the settings endpoints read from the path we just set.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import carrel.main as main_mod
from carrel.config import load_settings


# -------- Fixtures --------


@pytest.fixture()
def tmp_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the running app at a fresh YAML inside tmp_path.

    We rebind ``carrel.main.CONFIG_PATH`` (used by the settings router for
    late imports) and reset the module-level ``app_config`` / ``app_env`` so
    the next request picks up the new path. The lifespan has already booted
    against the real path; we don't restart it because the only consumer
    that matters for these tests is the settings router, which reads
    ``CONFIG_PATH`` lazily on every call.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("", encoding="utf-8")

    monkeypatch.setattr(main_mod, "CONFIG_PATH", cfg_path)
    new_cfg, new_env = load_settings(cfg_path)
    monkeypatch.setattr(main_mod, "app_config", new_cfg)
    monkeypatch.setattr(main_mod, "app_env", new_env)
    return cfg_path


def _seed(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


# -------- GET --------


def test_get_settings_returns_all_sections(client, tmp_config: Path):
    r = client.get("/settings")
    assert r.status_code == 200, r.text
    data = r.json()

    expected_sections = {
        "storage", "http", "cors", "openalex", "arxiv", "semantic_scholar",
        "llm", "embeddings", "mineru", "chunking", "download", "schedule",
        "mcp",
    }
    assert set(data["sections"]) == expected_sections
    assert data["yaml_path"] == str(tmp_config)
    # Every section carries a values dict and a (possibly empty) env_overrides
    for name, section in data["sections"].items():
        assert "values" in section, name
        assert "env_overrides" in section, name
        assert "requires_restart" in section, name
    # Env list is non-empty and the secret rows carry no value
    assert data["env"], "env summary list is empty"
    for entry in data["env"]:
        if entry["is_secret"]:
            assert entry["value"] is None, entry
    # Restart-required sections are reported
    assert set(data["restart_required_sections"]) == {"storage", "http", "cors", "mcp"}


def test_get_settings_masks_yaml_secrets(client, tmp_config: Path):
    _seed(tmp_config, "openalex:\n  api_key: sk-secret-xyz\n  mailto: a@b.c\n")
    # Re-bootstrap app_config to pick up the new YAML.
    cfg, env = load_settings(tmp_config)
    main_mod.app_config = cfg
    main_mod.app_env = env

    r = client.get("/settings")
    assert r.status_code == 200
    body = r.json()
    openalex = body["sections"]["openalex"]["values"]
    assert openalex["api_key"] == "***"
    assert "sk-secret-xyz" not in r.text  # never echoed
    # Non-secret fields are surfaced normally
    assert openalex["mailto"] == "a@b.c"


def test_get_settings_env_status_reflects_dotenv(client, tmp_config: Path, monkeypatch: pytest.MonkeyPatch):
    # Force a known set/unset pair without depending on the dev .env file.
    # We pick two keys and assert based on the values we control.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    monkeypatch.setenv("VOLCANO_API_KEY", "")  # empty string is treated as unset

    # Re-bootstrap so the new env values are captured by EnvSettings.
    cfg, env = load_settings(tmp_config)
    main_mod.app_config = cfg
    main_mod.app_env = env

    r = client.get("/settings")
    assert r.status_code == 200
    rows = {row["name"]: row for row in r.json()["env"]}
    assert rows["deepseek_api_key"]["is_set"] is True
    assert rows["deepseek_api_key"]["value"] is None  # masked
    assert rows["volcano_api_key"]["is_set"] is False  # empty string -> False
    # Non-secret env row carries the value
    if "summarize_model" in rows:
        assert rows["summarize_model"]["is_secret"] is False


# -------- PATCH --------


def test_patch_settings_updates_one_block(client, tmp_config: Path):
    r = client.patch(
        "/settings",
        json={"sections": {"arxiv": {"delay_between_requests_seconds": 5.5, "max_retries": 7}}},
    )
    assert r.status_code == 200, r.text
    arxiv = r.json()["sections"]["arxiv"]["values"]
    assert arxiv["delay_between_requests_seconds"] == 5.5
    assert arxiv["max_retries"] == 7

    # Other blocks untouched in-memory and on disk.
    on_disk = tmp_config.read_text(encoding="utf-8")
    assert "openalex" not in on_disk  # never touched
    assert "llm" not in on_disk
    assert main_mod.app_config.openalex.request_timeout_seconds == 30  # default


def test_patch_settings_rejects_invalid_value(client, tmp_config: Path):
    _seed(tmp_config, "arxiv:\n  max_retries: 3\n")
    cfg, env = load_settings(tmp_config)
    main_mod.app_config = cfg
    main_mod.app_env = env

    r = client.patch(
        "/settings",
        json={"sections": {"arxiv": {"max_retries": "not-an-int"}}},
    )
    assert r.status_code == 400, r.text
    # On-disk YAML must be byte-identical to the seed.
    assert tmp_config.read_text(encoding="utf-8") == "arxiv:\n  max_retries: 3\n"


def test_patch_settings_writes_atomically_on_bad_cron(client, tmp_config: Path):
    _seed(tmp_config, "schedule:\n  enabled: false\n  sync_cron: '0 8 * * *'\n")
    cfg, env = load_settings(tmp_config)
    main_mod.app_config = cfg
    main_mod.app_env = env

    r = client.patch(
        "/settings",
        json={"sections": {"schedule": {"enabled": True, "sync_cron": "banana"}}},
    )
    assert r.status_code == 400
    # YAML untouched (save_raw never ran for this block)
    assert tmp_config.read_text(encoding="utf-8") == (
        "schedule:\n  enabled: false\n  sync_cron: '0 8 * * *'\n"
    )


def test_patch_settings_rejects_unknown_field(client, tmp_config: Path):
    r = client.patch(
        "/settings",
        json={"sections": {"arxiv": {"made_up_field": 1}}},
    )
    assert r.status_code == 400
    assert "made_up_field" in r.json()["detail"]


def test_patch_settings_rejects_unknown_top_level_section(client, tmp_config: Path):
    r = client.patch(
        "/settings",
        json={"sections": {"no_such_block": {"x": 1}}},
    )
    assert r.status_code == 400
    assert "no_such_block" in r.json()["detail"]


def test_patch_settings_storage_writes_but_does_not_mutate_inmemory(client, tmp_config: Path):
    _seed(tmp_config, "storage:\n  root: ./data\n  papers_subdir: papers\n")
    cfg, env = load_settings(tmp_config)
    main_mod.app_config = cfg
    main_mod.app_env = env

    before = main_mod.app_config.storage.root
    r = client.patch(
        "/settings",
        json={"sections": {"storage": {"root": "/totally/new/path"}}},
    )
    assert r.status_code == 200, r.text

    # In-memory app_config unchanged (restart required).
    assert main_mod.app_config.storage.root == before
    # On disk, however, the new value is persisted.
    on_disk = tmp_config.read_text(encoding="utf-8")
    assert "/totally/new/path" in on_disk
    # Response reflects the *current in-memory* value (old), so the UI
    # doesn't lie about what's live.
    resp_root = r.json()["sections"]["storage"]["values"]["root"]
    assert resp_root == str(before)


def test_patch_settings_env_overridden_field_flagged(client, tmp_config: Path):
    r = client.get("/settings")
    sd = r.json()["sections"]
    # Each env-overrideable field is reported as {env_var, env_value?}. The
    # env_value is the live value the process is using (None when the env
    # var isn't actually set OR when the env var carries a secret).
    llm_ov = sd["llm"]["env_overrides"]
    assert llm_ov["summarize_model"]["env_var"] == "SUMMARIZE_MODEL"
    # The dev .env sets SUMMARIZE_MODEL=anthropic/claude-haiku-4-5; the
    # override must surface that value, not the YAML default.
    assert llm_ov["summarize_model"]["env_value"] == "anthropic/claude-haiku-4-5"
    assert llm_ov["fallback_model"]["env_var"] == "FALLBACK_MODEL"
    assert llm_ov["fallback_model"]["env_value"] == "anthropic/claude-haiku-4-5"
    assert sd["embeddings"]["env_overrides"]["model"]["env_var"] == "EMBEDDING_MODEL"
    assert sd["mineru"]["env_overrides"]["base_url"]["env_var"] == "MINERU_BASE_URL"
    assert sd["http"]["env_overrides"]["port"]["env_var"] == "CARREL_PORT"
    assert sd["http"]["env_overrides"]["host"]["env_var"] == "CARREL_HOST"
    assert sd["cors"]["env_overrides"]["origins"]["env_var"] == "CARREL_CORS_ORIGINS"
    # The value field on a secret env override must always be None — we
    # never echo a secret string back to the browser, even on the override.
    # (Only relevant when the env var is actually set; the assertion is
    # conditional so the test still passes in a dev .env that doesn't
    # configure an OpenAlex / S2 key.)
    if sd["openalex"]["env_overrides"].get("api_key"):
        assert sd["openalex"]["env_overrides"]["api_key"]["env_value"] is None
    if sd["semantic_scholar"]["env_overrides"].get("api_key"):
        assert sd["semantic_scholar"]["env_overrides"]["api_key"]["env_value"] is None


def test_get_settings_env_override_omitted_when_unset(client, tmp_config: Path, monkeypatch: pytest.MonkeyPatch):
    """If the env var isn't actually set, the field must NOT show as overridden.

    Otherwise the UI would render a misleading "from X" badge on a field
    whose value is just the YAML default. We delenv the override vars so
    the API's ``os.environ`` check sees them as absent, then re-bind
    app_env so app_config still reflects a valid (env-less) state.
    """
    for v in ("SUMMARIZE_MODEL", "FALLBACK_MODEL", "EMBEDDING_MODEL", "EMBEDDING_DIM",
              "MINERU_BASE_URL", "CARREL_HOST", "CARREL_PORT", "CARREL_CORS_ORIGINS",
              "OPENALEX_MAILTO", "OPENALEX_API_KEY", "S2_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    fresh_cfg, fresh_env = load_settings(tmp_config)
    main_mod.app_config = fresh_cfg
    main_mod.app_env = fresh_env

    r = client.get("/settings")
    sd = r.json()["sections"]
    assert sd["llm"]["env_overrides"] == {}
    assert sd["embeddings"]["env_overrides"] == {}
    assert sd["mineru"]["env_overrides"] == {}
    assert sd["http"]["env_overrides"] == {}
    assert sd["cors"]["env_overrides"] == {}
    assert sd["openalex"]["env_overrides"] == {}
    assert sd["semantic_scholar"]["env_overrides"] == {}


def test_patch_settings_subscriptions_replaces_list(client, tmp_config: Path):
    r = client.patch(
        "/settings",
        json={"sections": {"subscriptions": [
            {"kind": "keyword", "value": "rag", "label": "RAG"},
            {"kind": "arxiv_category", "value": "cs.CL", "label": "NLP"},
        ]}},
    )
    assert r.status_code == 200, r.text
    on_disk = tmp_config.read_text(encoding="utf-8")
    assert "kind: keyword" in on_disk
    assert "cs.CL" in on_disk
    # Invalid item rejected
    r = client.patch(
        "/settings",
        json={"sections": {"subscriptions": [{"kind": "nope", "value": "x"}]}},
    )
    assert r.status_code == 400


# -------- Regression for the update_schedule refactor --------


def test_schedule_patches_still_work(client, tmp_config: Path):
    """Defends the config_store refactor: the legacy /schedule PATCH
    path must still validate, parse crons, and atomically write."""
    r = client.patch(
        "/schedule",
        json={"enabled": True, "sync_cron": "0 9 * * *"},
    )
    assert r.status_code == 200, r.text
    on_disk = tmp_config.read_text(encoding="utf-8")
    assert "enabled: true" in on_disk
    # PyYAML may or may not quote cron strings depending on the parser;
    # match the unquoted form which is what safe_dump actually emits.
    assert "sync_cron: 0 9 * * *" in on_disk

    # Bad cron still rejected
    r = client.patch(
        "/schedule",
        json={"enabled": True, "sync_cron": "not a cron"},
    )
    assert r.status_code == 400


# -------- LLM output_language round-trip --------


def test_patch_settings_output_language_persists(client, tmp_config: Path):
    """The LLM card's output_language field is read by paper_card and
    summarize at call time. Round-trip it through PATCH /settings so
    a typo in the frontend selector would fail validation here
    instead of silently producing the wrong-language LLM output."""
    r = client.patch(
        "/settings",
        json={"sections": {"llm": {"output_language": "en"}}},
    )
    assert r.status_code == 200, r.text
    llm_block = r.json()["sections"]["llm"]["values"]
    assert llm_block["output_language"] == "en"

    # Persisted to disk.
    on_disk = tmp_config.read_text(encoding="utf-8")
    assert "output_language: en" in on_disk

    # And reflected in the live in-memory config — this is the path
    # the LLM call sites read, so a missing in-memory write would
    # be the silent failure mode we're guarding against.
    from carrel import main as main_mod
    assert main_mod.app_config.llm.output_language == "en"


def test_patch_settings_output_language_rejects_unknown_value(client, tmp_config: Path):
    r = client.patch(
        "/settings",
        json={"sections": {"llm": {"output_language": "fr"}}},
    )
    # Literal["zh","en"] in the Pydantic model → 400.
    assert r.status_code == 400, r.text
    on_disk = tmp_config.read_text(encoding="utf-8")
    # The bad value must NOT have been written.
    assert "output_language: fr" not in on_disk
