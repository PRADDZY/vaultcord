"""Shared constants for VaultCord."""

from __future__ import annotations

from typing import Final

APP_NAME: Final[str] = "VaultCord"
APP_DIR_NAME: Final[str] = ".vaultcord"
CONFIG_FILE_NAME: Final[str] = "config.json"
DB_FILE_NAME: Final[str] = "vaultcord.db"
LOG_FILE_NAME: Final[str] = "vaultcord.log"

DISCORD_API_BASE: Final[str] = "https://discord.com/api/v9"
VAULT_PREFIX: Final[str] = "vault://"

DEFAULT_REQUEST_TIMEOUT: Final[float] = 20.0
DEFAULT_MAX_RETRIES: Final[int] = 3

STATUS_PENDING: Final[str] = "pending"
STATUS_DONE: Final[str] = "done"
STATUS_FAILED: Final[str] = "failed"

MODE_ALL: Final[str] = "all"
MODE_TEXT: Final[str] = "text"
MODE_LINKS: Final[str] = "links"
MODE_MEDIA: Final[str] = "media"
VALID_MODES: Final[set[str]] = {MODE_ALL, MODE_TEXT, MODE_LINKS, MODE_MEDIA}

ORDER_NEWEST: Final[str] = "newest"
ORDER_OLDEST: Final[str] = "oldest"
VALID_ORDER_DIRECTIONS: Final[set[str]] = {ORDER_NEWEST, ORDER_OLDEST}

CHANNEL_TEXT_TYPES: Final[set[int]] = {0, 5}
CHANNEL_THREAD_TYPES: Final[set[int]] = {10, 11, 12}
