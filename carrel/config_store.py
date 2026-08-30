"""Persist runtime config to the active YAML file.

`PATCH /schedule` and `PATCH /settings` both need to write back to the same
YAML file the server booted from (``data/config.yaml`` by default). We load
the raw dict, update the relevant block, validate it against the matching
Pydantic model, then atomically write it back.

``update_yaml_sections`` is the generic entry point — ``update_schedule`` is
a thin wrapper kept so the existing schedule PATCH keeps its public
signature and call sites.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from carrel.config import (
    ArxivConfig,
    CarrelYAML,
    ChunkingConfig,
    CorsConfig,
    CrossrefConfig,
    DownloadConfig,
    EmbeddingsConfig,
    HttpConfig,
    LLMConfig,
    MCPConfig,
    MinerUConfig,
    OpenAlexConfig,
    ScheduleConfig,
    SemanticScholarConfig,
    StorageConfig,
    Subscription,
)

logger = logging.getLogger("carrel.config_store")

# Every YAML top-level block, keyed by its YAML name, mapped to its Pydantic
# model. Used by ``update_yaml_sections`` to merge + re-validate per section,
# and by ``api/settings.py`` to render + accept section updates.
SECTION_MODELS: dict[str, type[BaseModel]] = {
    "storage":          StorageConfig,
    "http":             HttpConfig,
    "cors":             CorsConfig,
    "openalex":         OpenAlexConfig,
    "arxiv":            ArxivConfig,
    "crossref":         CrossrefConfig,
    "semantic_scholar": SemanticScholarConfig,
    "llm":              LLMConfig,
    "embeddings":       EmbeddingsConfig,
    "mineru":           MinerUConfig,
    "chunking":         ChunkingConfig,
    "download":         DownloadConfig,
    "schedule":         ScheduleConfig,
    "mcp":              MCPConfig,
}

# Sections whose change in the running process requires a full restart
# (baked into uvicorn / middleware / StaticFiles at startup). The settings
# API persists these to disk but does NOT mutate ``app_config`` for them;
# the user has to restart for the new value to take effect.
RESTART_REQUIRED_SECTIONS: set[str] = {"storage", "http", "cors", "mcp"}

# Env-overrideable field names per section. The Settings UI shows these as
# read-only with an "overridden by VAR" chip — env wins at startup time
# (see ``carrel.config.load_settings``), so editing the YAML field would
# have no effect until the env var is unset.
ENV_OVERRIDE_FIELDS: dict[str, dict[str, str]] = {
    "openalex":         {"mailto": "OPENALEX_MAILTO",
                         "api_key": "OPENALEX_API_KEY"},
    "crossref":         {"mailto": "CROSSREF_MAILTO"},
    "semantic_scholar": {"api_key": "S2_API_KEY"},
    "llm":              {"summarize_model": "SUMMARIZE_MODEL",
                         "fallback_model":  "FALLBACK_MODEL"},
    "embeddings":       {"model": "EMBEDDING_MODEL",
                         "dim":   "EMBEDDING_DIM"},
    "mineru":           {"base_url": "MINERU_BASE_URL"},
    "http":             {"host": "CARREL_HOST",
                         "port": "CARREL_PORT"},
    "cors":             {"origins": "CARREL_CORS_ORIGINS"},
}

# The two YAML fields that hold a third-party API key. The settings API
# masks these on read so a frontend never sees the literal value.
SECRET_YAML_FIELDS: set[str] = {
    "openalex.api_key",
    "semantic_scholar.api_key",
}

# The keys the ``update_schedule`` shim accepts. Mirrored here so the legacy
# helper stays the single source of truth for the schedule PATCH body.
SCHEDULE_FIELD_KEYS: dict[str, type] = {
    "enabled": bool,
    "sync_cron": str,
    "remote_fill_enabled": bool,
    "remote_fill_cron": str,
    "publication_check_enabled": bool,
    "publication_check_cron": str,
    "wiki_compile_enabled": bool,
    "wiki_compile_cron": str,
}


class ConfigError(Exception):
    """Raised when the on-disk YAML cannot be read or written safely."""


def load_raw(path: Path) -> dict[str, Any]:
    """Read a YAML file into a dict. Missing file → empty dict."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level YAML must be a mapping")
    return data


def save_raw(path: Path, data: dict[str, Any]) -> None:
    """Atomically write ``data`` to ``path`` (temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".yaml", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
        os.replace(tmp_name, path)
    except Exception:
        # Best-effort cleanup of the temp file if os.replace never happened.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _validate_section_payload(
    section: str,
    body: dict[str, Any],
) -> tuple[dict[str, Any], type[BaseModel] | None]:
    """Validate ``body`` against the section's Pydantic model.

    Returns ``(merged_block, model)`` where ``merged_block`` is the
    serialised version of the validated model (the dict we'll write back).
    Raises ``ValueError`` for unknown section, unknown fields, or bad types.
    """
    if section == "subscriptions":
        if not isinstance(body, list):
            raise ValueError("subscriptions must be a list")
        for i, item in enumerate(body):
            Subscription.model_validate(item)  # raises on bad item
        return {"__list__": body}, None  # sentinel — caller handles the list

    model = SECTION_MODELS.get(section)
    if model is None:
        raise ValueError(f"unknown section: {section!r}")

    # Strict field check first so a typo never silently drops a value.
    valid_fields = set(model.model_fields)
    unknown = set(body) - valid_fields
    if unknown:
        raise ValueError(
            f"{section}: unknown field(s): {sorted(unknown)}"
        )

    # Validate the payload as a partial update — Pydantic v2 accepts
    # model.model_validate({...}) and only complains about required-missing
    # if a field without a default is omitted. We then merge with the
    # on-disk block so defaults / untouched fields are preserved.
    validated = model.model_validate(body)
    return validated.model_dump(mode="json"), model


def _parse_schedule_cron(merged: dict[str, Any]) -> None:
    """Re-parse every enabled schedule cron so a typo 400s instead of
    silently taking down the scheduler on restart."""
    from apscheduler.triggers.cron import CronTrigger  # noqa: PLC0415

    for enabled_attr, cron_attr in (
        ("enabled", "sync_cron"),
        ("remote_fill_enabled", "remote_fill_cron"),
        ("publication_check_enabled", "publication_check_cron"),
        ("wiki_compile_enabled", "wiki_compile_cron"),
    ):
        if merged.get(enabled_attr):
            try:
                CronTrigger.from_crontab(merged[cron_attr])
            except (ValueError, KeyError) as e:
                raise ValueError(f"{cron_attr}: {e}") from e


def update_yaml_sections(
    path: Path,
    updates: dict[str, dict[str, Any] | list[Any]],
    *,
    schedule_cron_check: bool = True,
) -> dict[str, Any]:
    """Merge each section in ``updates`` into the YAML, validate, atomic-write.

    ``updates`` maps YAML section name → section body. Section body is a
    dict for ``SECTION_MODELS`` sections and a list for ``"subscriptions"``.
    Sections not present in ``updates`` are left untouched.

    Returns the post-merge sections dict, keyed by section name. For
    ``"subscriptions"`` the value is the list of subscription dicts (not
    wrapped in a dict).

    Raises ``ValueError`` for unknown sections, unknown fields, type
    mismatches, or invalid cron expressions. Raises ``OSError`` for IO
    failures (the API maps these to HTTP 500).
    """
    raw = load_raw(path)

    out: dict[str, Any] = {}
    for section, body in updates.items():
        if section == "subscriptions":
            merged_block, _ = _validate_section_payload(section, body)  # type: ignore[arg-type]
            raw["subscriptions"] = merged_block["__list__"]
            out[section] = merged_block["__list__"]
            continue

        merged_block, _ = _validate_section_payload(section, body)  # type: ignore[arg-type]
        if section == "schedule" and schedule_cron_check:
            _parse_schedule_cron(merged_block)
        raw[section] = merged_block
        out[section] = merged_block

    save_raw(path, raw)
    logger.info("settings written to %s: sections=%s", path, sorted(out))
    return out


def update_schedule(path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Backwards-compatible thin wrapper around ``update_yaml_sections``.

    Validates ``updates`` against the legacy ``SCHEDULE_FIELD_KEYS`` shape
    so existing callers (and the schedule PATCH body schema) keep their
    strict type check.
    """
    for key, value in updates.items():
        if key not in SCHEDULE_FIELD_KEYS:
            continue
        expected = SCHEDULE_FIELD_KEYS[key]
        if not isinstance(value, expected):
            raise ValueError(f"{key} must be {expected.__name__}")

    result = update_yaml_sections(path, {"schedule": updates})
    return result["schedule"]


# Re-export the full ``CarrelYAML`` model for callers that want a typed
# view of every supported section (the settings API uses this to list all
# sections it knows about).
__all__ = [
    "ConfigError",
    "CarrelYAML",
    "ENV_OVERRIDE_FIELDS",
    "RESTART_REQUIRED_SECTIONS",
    "SCHEDULE_FIELD_KEYS",
    "SECRET_YAML_FIELDS",
    "SECTION_MODELS",
    "load_raw",
    "save_raw",
    "update_schedule",
    "update_yaml_sections",
]
