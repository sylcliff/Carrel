"""Settings read/write endpoints.

`GET   /settings`   — current effective config: every YAML section plus a
                       read-only summary of `.env` (set/not-set, no values
                       for secrets).
`PATCH /settings`   — partial update keyed by YAML section. Each section body
                       is validated against the matching Pydantic model from
                       ``carrel.config``. Secret fields (api_key) are never
                       echo'd back; restart-required sections (``storage``,
                       ``http``, ``cors``) are persisted to disk but not
                       applied to the in-memory config.

Modeled on ``carrel.api.schedule`` — same late-import of ``app_config`` and
``CONFIG_PATH`` so the router reads the live values without DI gymnastics.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from carrel import scheduler as sched_mod
from carrel.config import CarrelYAML, EnvSettings, load_settings
from carrel.api._app_cache import cached
from carrel.api._invalidation import invalidate_settings_changed
from carrel.config_store import (
    ENV_OVERRIDE_FIELDS,
    RESTART_REQUIRED_SECTIONS,
    SECRET_YAML_FIELDS,
    SECTION_MODELS,
    update_yaml_sections,
)
from carrel.schemas import EnvEntry, EnvOverride, SerialisedSection, SettingsOut, SettingsUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

# Human-friendly labels for the .env keys exposed in the UI. Order is
# preserved in the response so the page renders the same way every time.
ENV_LABELS: list[tuple[str, str, bool]] = [
    # (attribute_name, label, is_secret)
    ("database_url",               "Postgres URL",            True),
    ("deepseek_api_key",           "DeepSeek API key",        True),
    ("volcano_api_key",            "Volcano Engine API key",  True),
    ("openalex_api_key",           "OpenAlex API key",        True),
    ("openalex_mailto",            "OpenAlex mailto",         False),
    ("s2_api_key",                 "Semantic Scholar API key", True),
    ("summarize_model",            "Summarizer model",        False),
    ("fallback_model",             "Fallback model",          False),
    ("embedding_model",            "Embedding model",         False),
    ("embedding_dim",              "Embedding dim",           False),
    ("mineru_base_url",            "MinerU base URL",         False),
    ("carrel_host",                "Carrel host",             False),
    ("carrel_port",                "Carrel port",             False),
    ("carrel_cors_origins",        "CORS origins",            False),
    ("remote_ssh_enabled",         "Remote SSH enabled",      False),
    ("remote_ssh_host",            "Remote SSH host",         False),
    ("remote_ssh_port",            "Remote SSH port",         False),
    ("remote_ssh_user",            "Remote SSH user",         False),
    ("remote_ssh_key_path",        "Remote SSH key path",     True),
    ("remote_ssh_known_hosts_path","Remote SSH known_hosts",  True),
    ("remote_ssh_connect_timeout", "Remote SSH connect timeout", False),
    ("remote_work_dir",            "Remote work dir",         False),
    ("remote_command_template",    "Remote command template", False),
    ("remote_dl_timeout",          "Remote download timeout", False),
    ("remote_retries",             "Remote retries",          False),
    ("remote_journal_min_age_days","Journal detection min age (days)", False),
    ("remote_journal_check_throttle_days", "Journal detection throttle (days)", False),
    ("brave_api_key",              "Brave Search API key (MCP)",   True),
    ("mcp_enabled",                "MCP integration enabled",      False),
]

# Env-var names that carry a secret. The override list (ENV_OVERRIDE_FIELDS)
# is keyed on the env-var name, so we map each secret EnvSettings attribute
# (lowercased form used in ENV_LABELS) to its env-var name. Used to decide
# whether to surface the live env value to the UI — we always suppress it
# for secret overrides.
ENV_SECRET_VARS: set[str] = {
    "OPENALEX_API_KEY",
    "S2_API_KEY",
    "DEEPSEEK_API_KEY",
    "VOLCANO_API_KEY",
    "DATABASE_URL",
    "REMOTE_SSH_KEY_PATH",
    "REMOTE_SSH_KNOWN_HOSTS_PATH",
    "BRAVE_API_KEY",
}


def _get_app_config() -> tuple[CarrelYAML, EnvSettings, Path]:
    """Late import to avoid the circular main -> api -> main chain."""
    from carrel.main import app_config, app_env, CONFIG_PATH  # noqa: PLC0415

    return app_config, app_env, CONFIG_PATH


def _mask_secrets(section: str, values: dict[str, Any]) -> dict[str, Any]:
    """Replace secret leaves in ``values`` with ``"***"`` for read responses.

    Walks the dotted paths in ``SECRET_YAML_FIELDS`` whose section prefix
    matches ``section``. Mutates a shallow copy of ``values`` so callers
    holding the original (e.g. the in-memory config) are unaffected.
    """
    out = dict(values)
    for dotted in SECRET_YAML_FIELDS:
        head, _, leaf = dotted.partition(".")
        if head != section or leaf not in out:
            continue
        # Only mask when the value is truthy; leave ``None`` / empty as-is
        # so the UI can still tell "not set" from "set".
        if out[leaf]:
            out[leaf] = "***"
    return out


def _serialise_section(
    name: str,
    cfg: CarrelYAML,
    env: EnvSettings,
) -> SerialisedSection:
    """Materialise one section of the config as the API's read shape.

    For each field listed in ``ENV_OVERRIDE_FIELDS[name]`` we check whether
    the env var is actually present in the current process environment
    (``os.environ``) — using ``app_env`` alone can't distinguish "set in
    env" from "EnvSettings default", which would mis-report every override
    field as overridden even when the user never touched the .env. When the
    env var is set and not flagged as a secret, we also include the live
    value so the UI can show the source of truth.
    """
    import os

    model = SECTION_MODELS.get(name)
    if model is None:
        return SerialisedSection()
    section_obj = getattr(cfg, name)
    values = _mask_secrets(name, model.model_validate(section_obj).model_dump(mode="json"))
    overrides: dict[str, EnvOverride] = {}
    for field, env_var in ENV_OVERRIDE_FIELDS.get(name, {}).items():
        if env_var not in os.environ:
            continue
        raw = os.environ[env_var]
        if env_var in ENV_SECRET_VARS:
            # We know it's set, but never echo the secret back.
            overrides[field] = EnvOverride(env_var=env_var, env_value=None)
            continue
        # Empty string in .env usually means "set, but no value"; treat it
        # the same as unset so the UI doesn't show a misleading "" override.
        if raw == "":
            continue
        overrides[field] = EnvOverride(env_var=env_var, env_value=raw)
    return SerialisedSection(
        values=values,
        env_overrides=overrides,
        requires_restart=name in RESTART_REQUIRED_SECTIONS,
    )


def _env_entries(env: EnvSettings) -> list[EnvEntry]:
    """Build the read-only .env summary rows."""
    rows: list[EnvEntry] = []
    for attr, label, is_secret in ENV_LABELS:
        if not hasattr(env, attr):
            continue
        raw = getattr(env, attr)
        if is_secret:
            rows.append(EnvEntry(
                name=attr, label=label, is_secret=True,
                is_set=bool(raw), value=None,
            ))
        else:
            # Non-secret: surface the value so the user can sanity-check
            # which MinerU / which port etc. is wired in.
            rendered = None if raw is None else str(raw)
            rows.append(EnvEntry(
                name=attr, label=label, is_secret=False,
                is_set=rendered is not None and rendered != "",
                value=rendered,
            ))
    return rows


def _build_response(
    cfg: CarrelYAML,
    env: EnvSettings,
    yaml_path: Path,
) -> SettingsOut:
    sections: dict[str, SerialisedSection] = {}
    restart: list[str] = []
    for name in SECTION_MODELS:
        sections[name] = _serialise_section(name, cfg, env)
        if name in RESTART_REQUIRED_SECTIONS:
            restart.append(name)
    return SettingsOut(
        yaml_path=str(yaml_path),
        sections=sections,
        env=_env_entries(env),
        restart_required_sections=restart,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@cached("settings", tags=("settings",))
def _get_settings_body() -> SettingsOut:
    cfg, env, path = _get_app_config()
    return _build_response(cfg, env, path)


@router.get("", response_model=SettingsOut)
def get_settings(response: Response) -> SettingsOut:
    """Current effective config.

    Layer 1: settings change rarely and the file's mtime is a stable
    proxy, but env vars can change without a write. Use a short
    max-age and rely on L2 invalidation (Phase 3) for the PATCH path.
    """
    response.headers["Cache-Control"] = "private, max-age=5, stale-while-revalidate=15"
    return _get_settings_body()


@router.patch("", response_model=SettingsOut)
def patch_settings(body: SettingsUpdate) -> SettingsOut:
    cfg, _env, config_path = _get_app_config()
    updates = body.sections
    if not updates:
        return _build_response(cfg, _env, config_path)

    # Unknown top-level sections fail fast — protects against typos in the
    # request body that would otherwise silently no-op.
    known = set(SECTION_MODELS) | {"subscriptions"}
    bad = set(updates) - known
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"unknown section(s): {sorted(bad)}",
        )

    path = Path(config_path)
    try:
        update_yaml_sections(
            path,
            updates,  # type: ignore[arg-type]
            schedule_cron_check="schedule" in updates,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OSError as e:
        logger.exception("failed to write %s", path)
        raise HTTPException(status_code=500, detail=f"could not write config: {e}") from e

    # Reload so the response reflects env-merged values and the on-disk
    # truth. Sections in RESTART_REQUIRED_SECTIONS keep their pre-PATCH
    # values in app_config (the change applies on next boot).
    new_cfg, new_env = load_settings(path)

    for section in updates:
        if section in RESTART_REQUIRED_SECTIONS:
            # Persisted, not applied — leave app_config alone.
            continue
        if section == "subscriptions":
            cfg.subscriptions = new_cfg.subscriptions
            continue
        setattr(cfg, section, getattr(new_cfg, section))

    # Re-arm modules that cache client / scheduler settings at startup.
    # Each is wrapped in try/except so a misconfigured value (e.g. an
    # unreachable base_url) doesn't 500 the whole PATCH.
    if "semantic_scholar" in updates or "http" in updates:
        try:
            from carrel.sources import semanticscholar_client as s2  # noqa: PLC0415

            s2.configure(
                base_url=cfg.semantic_scholar.base_url,
                api_key=cfg.semantic_scholar.api_key,
                timeout=cfg.semantic_scholar.request_timeout_seconds,
                max_retries=cfg.semantic_scholar.max_retries,
                rate_limit_per_second=cfg.semantic_scholar.rate_limit_per_second,
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("failed to reconfigure semanticscholar_client after settings PATCH")

    if "openalex" in updates:
        try:
            from carrel.sources import openalex_client as oa  # noqa: PLC0415

            oa.configure(cfg)
        except Exception:  # pragma: no cover - defensive
            logger.exception("failed to reconfigure openalex_client after settings PATCH")

    if "schedule" in updates:
        try:
            sched_mod.restart_scheduler(cfg)
        except Exception:  # pragma: no cover - defensive
            logger.exception("failed to restart scheduler after settings PATCH")

    # L2: drop the cached settings response. The new env-merged values
    # are now in ``cfg``; the next GET rebuilds the body from scratch.
    invalidate_settings_changed()
    return _build_response(cfg, new_env, path)
