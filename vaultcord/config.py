"""Configuration loading and defaults."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .constants import (
    APP_DIR_NAME,
    CONFIG_FILE_NAME,
    DB_FILE_NAME,
    DEFAULT_MAX_RETRIES,
    DEFAULT_REQUEST_TIMEOUT,
    LOG_FILE_NAME,
)
from .models import AppConfig, SchedulerConfig


def default_data_dir() -> Path:
    return Path.home() / APP_DIR_NAME


def default_config_path() -> Path:
    return default_data_dir() / CONFIG_FILE_NAME


def default_db_path() -> Path:
    return default_data_dir() / DB_FILE_NAME


def default_log_path() -> Path:
    return default_data_dir() / LOG_FILE_NAME


def _expand_path(value: str) -> str:
    return str(Path(value).expanduser())


def _default_config() -> dict:
    scheduler = SchedulerConfig()
    return {
        "data_dir": str(default_data_dir()),
        "db_path": str(default_db_path()),
        "log_path": str(default_log_path()),
        "request_timeout_seconds": DEFAULT_REQUEST_TIMEOUT,
        "max_retries": DEFAULT_MAX_RETRIES,
        "scheduler": asdict(scheduler),
    }


def ensure_config() -> Path:
    config_path = default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        with config_path.open("w", encoding="utf-8") as handle:
            json.dump(_default_config(), handle, indent=2)
    return config_path


def load_config() -> AppConfig:
    config_path = ensure_config()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    scheduler = raw.get("scheduler", {})
    scheduler_config = SchedulerConfig(
        edit_delay_min_seconds=int(scheduler.get("edit_delay_min_seconds", 15)),
        edit_delay_max_seconds=int(scheduler.get("edit_delay_max_seconds", 25)),
        run_hours_min=float(scheduler.get("run_hours_min", 1.5)),
        run_hours_max=float(scheduler.get("run_hours_max", 3.0)),
        pause_hours_min=float(scheduler.get("pause_hours_min", 0.5)),
        pause_hours_max=float(scheduler.get("pause_hours_max", 2.0)),
    )

    return AppConfig(
        data_dir=_expand_path(str(raw.get("data_dir", default_data_dir()))),
        db_path=_expand_path(str(raw.get("db_path", default_db_path()))),
        log_path=_expand_path(str(raw.get("log_path", default_log_path()))),
        request_timeout_seconds=float(raw.get("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT)),
        max_retries=int(raw.get("max_retries", DEFAULT_MAX_RETRIES)),
        scheduler=scheduler_config,
    )
