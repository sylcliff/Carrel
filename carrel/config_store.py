"""Persist schedule settings to the active YAML config file.

`PATCH /schedule` needs to write back to the same YAML file that the server
booted from (``data/config.yaml`` by default). We load the raw dict, update
only the ``schedule:`` block, validate it against ``ScheduleConfig``, then
atomically write it back.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from carrel.config import ScheduleConfig

logger = logging.getLogger("carrel.config_store")

# Fields we accept on the PATCH /schedule body, mapped to their YAML keys.
FIELD_KEYS: dict[str, type] = {
    "enabled": bool,
    "sync_cron": str,
    "remote_fill_enabled": bool,
    "remote_fill_cron": str,
    "publication_check_enabled": bool,
    "publication_check_cron": str,
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


def update_schedule(path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` into the schedule block and persist.

    Returns the new schedule block as a plain dict (as persisted).
    Raises ``ConfigError`` on IO/parse errors and ``ValueError`` when the
    merged result fails ``ScheduleConfig`` validation.
    """
    raw = load_raw(path)
    schedule = raw.get("schedule")
    if not isinstance(schedule, dict):
        schedule = {}

    for key, value in updates.items():
        if key not in FIELD_KEYS:
            continue
        expected = FIELD_KEYS[key]
        if not isinstance(value, expected):
            raise ValueError(f"{key} must be {expected.__name__}")
        schedule[key] = value

    # Validate the merged block against the same Pydantic model the app uses
    # at startup — catches wrong types / missing fields.
    validated = ScheduleConfig.model_validate(schedule)

    # Re-parse every configured cron string with APScheduler so a typo fails
    # the request rather than silently taking down the scheduler on restart.
    from apscheduler.triggers.cron import CronTrigger  # noqa: PLC0415

    for enabled_attr, cron_attr in (
        ("enabled", "sync_cron"),
        ("remote_fill_enabled", "remote_fill_cron"),
        ("publication_check_enabled", "publication_check_cron"),
    ):
        if getattr(validated, enabled_attr):
            try:
                CronTrigger.from_crontab(getattr(validated, cron_attr))
            except ValueError as e:
                raise ValueError(f"{cron_attr}: {e}") from e

    raw["schedule"] = schedule
    save_raw(path, raw)
    logger.info("schedule config written to %s: %s", path, schedule)
    return schedule
