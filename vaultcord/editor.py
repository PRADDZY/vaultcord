"""Message editing and vault-reference utilities."""

from __future__ import annotations

import secrets

from .constants import VAULT_PREFIX
from .discord_api import DiscordClient


def generate_vault_id() -> str:
    return secrets.token_urlsafe(9)


def make_reference(vault_id: str) -> str:
    return f"{VAULT_PREFIX}{vault_id}"


async def apply_vault_reference(
    client: DiscordClient,
    *,
    channel_id: str,
    message_id: str,
    vault_id: str,
) -> None:
    await client.edit_message(channel_id=channel_id, message_id=message_id, content=make_reference(vault_id))
