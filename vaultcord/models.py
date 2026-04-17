"""Typed models for VaultCord runtime and persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class SchedulerConfig:
    edit_delay_min_seconds: int = 15
    edit_delay_max_seconds: int = 25
    run_hours_min: float = 1.5
    run_hours_max: float = 3.0
    pause_hours_min: float = 0.5
    pause_hours_max: float = 2.0


@dataclass(slots=True)
class AppConfig:
    data_dir: str
    db_path: str
    log_path: str
    request_timeout_seconds: float
    max_retries: int
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)


@dataclass(slots=True)
class AuthContext:
    user_id: str
    username: str
    encrypted_token: str
    token_salt_b64: str


@dataclass(slots=True)
class VaultSession:
    user_id: str
    username: str
    token: str
    password: str


@dataclass(slots=True)
class ScrapedMessage:
    message_id: str
    channel_id: str
    guild_id: str | None
    author_id: str
    content: str
    attachments: list[dict[str, Any]]
    timestamp: str
    channel_type: int | None = None


@dataclass(slots=True)
class QueueJob:
    id: int
    discord_message_id: str
    channel_id: str
    guild_id: str | None
    mode: str
    status: str
    attempts: int
    next_attempt_at: str | None
    last_error: str | None
    vault_id: str


@dataclass(slots=True)
class WorkerStats:
    total: int = 0
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    started_at: datetime | None = None

    @property
    def remaining(self) -> int:
        return max(self.total - self.processed - self.failed, 0)
