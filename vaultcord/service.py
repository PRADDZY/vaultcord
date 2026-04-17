"""Core service orchestration for login, queue prep, and retrieval."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from .constants import MODE_ALL, ORDER_NEWEST, ORDER_OLDEST, STATUS_DONE, STATUS_FAILED, STATUS_PENDING, VAULT_PREFIX
from .discord_api import DiscordClient, DiscordApiError
from .editor import generate_vault_id, make_reference
from .models import AppConfig, VaultSession
from .scraper import MessageScraper
from .security import decrypt_message_payload, decrypt_token, encrypt_message_payload, encrypt_token
from .storage import VaultStore

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PrepareResult:
    queued: int
    skipped: int
    already_referenced: int


class VaultService:
    def __init__(self, config: AppConfig, store: VaultStore) -> None:
        self.config = config
        self.store = store

    async def login(self, token: str, password: str) -> dict[str, str]:
        async with DiscordClient(token=token, timeout_seconds=self.config.request_timeout_seconds) as client:
            me = await client.get_me()

        encrypted = encrypt_token(token=token, password=password)
        payload = {
            "user_id": str(me["id"]),
            "username": f"{me.get('username', '')}#{me.get('discriminator', '0')}",
            **encrypted,
        }
        self.store.save_setting("auth", payload)
        return {"user_id": payload["user_id"], "username": payload["username"]}

    def unlock_session(self, password: str) -> VaultSession:
        auth = self.store.read_setting("auth")
        if not auth:
            raise RuntimeError("No stored login found. Run `vault login` first.")
        token = decrypt_token(auth, password)
        return VaultSession(
            user_id=str(auth["user_id"]),
            username=str(auth["username"]),
            token=token,
            password=password,
        )

    async def preview_counts(self, session: VaultSession, guild_id: str) -> dict[str, int]:
        counts: Counter[str] = Counter()
        async with DiscordClient(token=session.token, timeout_seconds=self.config.request_timeout_seconds) as client:
            scraper = MessageScraper(client=client, user_id=session.user_id)
            async for message in scraper.iter_user_messages(guild_id=guild_id, mode=MODE_ALL):
                message_mode = scraper.detect_mode(
                    {"content": message.content, "attachments": message.attachments}
                )
                counts[message_mode] += 1
                counts["all"] += 1

        return {
            "all": int(counts["all"]),
            "text": int(counts["text"]),
            "links": int(counts["links"]),
            "media": int(counts["media"]),
        }

    async def prepare_jobs(
        self,
        session: VaultSession,
        *,
        guild_id: str,
        mode: str,
        order_direction: str = ORDER_NEWEST,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> PrepareResult:
        if order_direction not in {ORDER_NEWEST, ORDER_OLDEST}:
            raise ValueError(f"Unsupported order direction: {order_direction}")
        queued = 0
        skipped = 0
        already_referenced = 0

        async with DiscordClient(token=session.token, timeout_seconds=self.config.request_timeout_seconds) as client:
            scraper = MessageScraper(client=client, user_id=session.user_id)

            async for message in scraper.iter_user_messages(guild_id=guild_id, mode=mode):
                if message.content.startswith(VAULT_PREFIX):
                    already_referenced += 1
                    if event_sink:
                        event_sink({
                            "type": "log",
                            "level": "SKIP",
                            "message": f"{message.message_id} already contains vault reference",
                        })
                    continue

                if self.store.vault_exists_for_message(message.message_id):
                    skipped += 1
                    continue

                payload = {
                    "message_id": message.message_id,
                    "channel_id": message.channel_id,
                    "guild_id": message.guild_id,
                    "author_id": message.author_id,
                    "content": message.content,
                    "attachments": message.attachments,
                    "timestamp": message.timestamp,
                    "archived_at": datetime.now(UTC).isoformat(),
                }

                encrypted_payload = encrypt_message_payload(payload, password=session.password)
                vault_id = generate_vault_id()
                inserted_message = self.store.insert_archived_message(
                    vault_id=vault_id,
                    discord_message_id=message.message_id,
                    channel_id=message.channel_id,
                    guild_id=message.guild_id,
                    author_id=message.author_id,
                    mode=mode,
                    reference_text=make_reference(vault_id),
                    encrypted_payload=encrypted_payload,
                )
                if not inserted_message:
                    skipped += 1
                    continue

                self.store.enqueue_job(
                    discord_message_id=message.message_id,
                    channel_id=message.channel_id,
                    guild_id=message.guild_id,
                    mode=mode,
                    vault_id=vault_id,
                    priority=self._message_priority(message.message_id),
                    status=STATUS_PENDING,
                )
                queued += 1

                if event_sink and queued % 50 == 0:
                    event_sink({
                        "type": "log",
                        "level": "OK",
                        "message": f"Prepared {queued} messages so far",
                    })

        return PrepareResult(queued=queued, skipped=skipped, already_referenced=already_referenced)

    @staticmethod
    def _message_priority(message_id: str) -> int | None:
        try:
            return int(message_id)
        except (TypeError, ValueError):
            return None

    def decrypt_vault_message(self, vault_id: str, password: str) -> dict[str, Any]:
        encrypted = self.store.get_encrypted_message(vault_id)
        if not encrypted:
            raise RuntimeError(f"Vault id not found: {vault_id}")
        return decrypt_message_payload(encrypted, password=password)

    async def validate_session(self, session: VaultSession) -> None:
        async with DiscordClient(token=session.token, timeout_seconds=self.config.request_timeout_seconds) as client:
            try:
                await client.get_me()
            except DiscordApiError as exc:
                raise RuntimeError("Stored token is no longer valid.") from exc

    def retry_failed(self, guild_id: str | None, mode: str | None) -> int:
        return self.store.reset_failed_jobs(guild_id=guild_id, mode=mode)

    def has_retryable_queue(self, guild_id: str | None, mode: str | None) -> bool:
        return self.store.has_retryable_work(
            guild_id=guild_id,
            mode=mode,
            max_attempts=self.config.max_retries,
            retry_failed_only=False,
        )

    def progress(self, guild_id: str | None, mode: str | None) -> dict[str, int]:
        return self.store.get_progress(
            guild_id=guild_id,
            mode=mode,
            max_attempts=self.config.max_retries,
        )
