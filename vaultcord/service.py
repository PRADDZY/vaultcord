"""Core service orchestration for login, queue prep, and retrieval."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from .constants import MODE_ALL, ORDER_NEWEST, ORDER_OLDEST, STATUS_PENDING, VAULT_PREFIX
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
    failed_channels: int = 0
    fetch_error_breakdown: dict[str, int] = field(default_factory=dict)
    exhausted: bool = False


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
        queued = 0
        skipped = 0
        already_referenced = 0
        failed_channels = 0
        fetch_error_breakdown: Counter[str] = Counter()
        exhausted = False

        while True:
            batch = await self.prepare_jobs_batch(
                session,
                guild_id=guild_id,
                mode=mode,
                order_direction=order_direction,
                batch_size=self.config.batch_prepare_size,
                event_sink=event_sink,
            )
            queued += batch.queued
            skipped += batch.skipped
            already_referenced += batch.already_referenced
            failed_channels += batch.failed_channels
            fetch_error_breakdown.update(batch.fetch_error_breakdown)
            exhausted = batch.exhausted
            if batch.exhausted:
                break
            if batch.queued == 0:
                break

        return PrepareResult(
            queued=queued,
            skipped=skipped,
            already_referenced=already_referenced,
            failed_channels=failed_channels,
            fetch_error_breakdown=dict(fetch_error_breakdown),
            exhausted=exhausted,
        )

    async def prepare_jobs_batch(
        self,
        session: VaultSession,
        *,
        guild_id: str,
        mode: str,
        order_direction: str = ORDER_NEWEST,
        batch_size: int = 1000,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> PrepareResult:
        if order_direction not in {ORDER_NEWEST, ORDER_OLDEST}:
            raise ValueError(f"Unsupported order direction: {order_direction}")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        queued = 0
        skipped = 0
        already_referenced = 0
        failed_channels = 0
        fetch_error_breakdown: Counter[str] = Counter()
        exhausted = False
        cursor_key = self._scrape_cursor_key(guild_id=guild_id, mode=mode, order_direction=order_direction)

        async with DiscordClient(token=session.token, timeout_seconds=self.config.request_timeout_seconds) as client:
            scraper = MessageScraper(client=client, user_id=session.user_id)
            state = self.store.read_setting(cursor_key)
            if not state:
                if event_sink:
                    event_sink(
                        {
                            "type": "log",
                            "level": "INFO",
                            "message": "Discovering channels/threads for initial scan...",
                        }
                    )

                discovery_last_logged = 0

                def _on_discovery_progress(done: int, total: int) -> None:
                    nonlocal discovery_last_logged
                    if not event_sink:
                        return
                    if total <= 0:
                        return
                    should_log = done == 1 or done == total or (done - discovery_last_logged >= 25)
                    if not should_log:
                        return
                    discovery_last_logged = done
                    event_sink(
                        {
                            "type": "log",
                            "level": "INFO",
                            "message": (
                                "Discovery progress: scanned "
                                f"{done}/{total} parent channels for archived threads"
                            ),
                        }
                    )

                channel_ids = await scraper.discover_channel_ids(
                    guild_id,
                    on_discovery_progress=_on_discovery_progress,
                )
                state = {
                    "version": 1,
                    "channel_ids": channel_ids,
                    "channel_index": 0,
                    "before_by_channel": {},
                }
                self.store.save_setting(cursor_key, state)
                if event_sink:
                    event_sink(
                        {
                            "type": "log",
                            "level": "INFO",
                            "message": f"Discovered {len(channel_ids)} channels/threads to scan",
                        }
                    )

            channel_ids = [str(v) for v in list(state.get("channel_ids", []))]
            channel_index = int(state.get("channel_index", 0))
            if channel_ids:
                channel_index = channel_index % len(channel_ids)
            else:
                channel_index = 0
            before_by_channel: dict[str, str] = {
                str(k): str(v) for k, v in dict(state.get("before_by_channel", {})).items()
            }
            pages_scanned = 0

            while channel_ids and queued < batch_size:
                channel_id = channel_ids[channel_index]
                before = before_by_channel.get(channel_id)
                try:
                    batch = await client.fetch_channel_messages(channel_id, before=before, limit=100)
                except DiscordApiError as exc:
                    status_key = str(exc.status_code) if exc.status_code is not None else "unknown"
                    failed_channels += 1
                    fetch_error_breakdown[status_key] += 1
                    LOGGER.debug("Skipping channel %s after API error status=%s", channel_id, status_key)
                    channel_ids.pop(channel_index)
                    before_by_channel.pop(channel_id, None)
                    if channel_ids and channel_index >= len(channel_ids):
                        channel_index = 0
                    continue

                if not batch:
                    channel_ids.pop(channel_index)
                    before_by_channel.pop(channel_id, None)
                    if channel_ids and channel_index >= len(channel_ids):
                        channel_index = 0
                    continue

                pages_scanned += 1
                if event_sink and pages_scanned % 25 == 0:
                    event_sink(
                        {
                            "type": "log",
                            "level": "INFO",
                            "message": (
                                f"Preparing batch: pages={pages_scanned} "
                                f"active_channels={len(channel_ids)} queued={queued}"
                            ),
                        }
                    )

                last_seen_id = str(batch[-1].get("id", "")) if batch else ""
                for raw_message in batch:
                    last_seen_id = str(raw_message.get("id", "")) or last_seen_id
                    author_id = str((raw_message.get("author") or {}).get("id") or "")
                    if author_id != session.user_id:
                        continue
                    message_mode = scraper.detect_mode(raw_message)
                    if not scraper.mode_matches(message_mode, mode):
                        continue

                    content = str(raw_message.get("content") or "")
                    if content.startswith(VAULT_PREFIX):
                        already_referenced += 1
                        continue

                    message_id = str(raw_message.get("id") or "")
                    if not message_id:
                        continue
                    if self.store.vault_exists_for_message(message_id):
                        skipped += 1
                        continue

                    payload = {
                        "message_id": message_id,
                        "channel_id": channel_id,
                        "guild_id": guild_id,
                        "author_id": author_id,
                        "content": content,
                        "attachments": list(raw_message.get("attachments") or []),
                        "timestamp": str(raw_message.get("timestamp") or ""),
                        "archived_at": datetime.now(UTC).isoformat(),
                    }
                    encrypted_payload = encrypt_message_payload(payload, password=session.password)
                    vault_id = generate_vault_id()
                    inserted_message = self.store.insert_archived_message(
                        vault_id=vault_id,
                        discord_message_id=message_id,
                        channel_id=channel_id,
                        guild_id=guild_id,
                        author_id=author_id,
                        mode=mode,
                        reference_text=make_reference(vault_id),
                        encrypted_payload=encrypted_payload,
                    )
                    if not inserted_message:
                        skipped += 1
                        continue

                    self.store.enqueue_job(
                        discord_message_id=message_id,
                        channel_id=channel_id,
                        guild_id=guild_id,
                        mode=mode,
                        vault_id=vault_id,
                        priority=self._message_priority(message_id),
                        status=STATUS_PENDING,
                    )
                    queued += 1
                    if event_sink and queued % 50 == 0:
                        event_sink(
                            {
                                "type": "log",
                                "level": "OK",
                                "message": f"Prepared {queued} messages in current batch",
                            }
                        )
                    if queued >= batch_size:
                        break

                if last_seen_id:
                    before_by_channel[channel_id] = last_seen_id
                if queued >= batch_size:
                    break
                if channel_ids:
                    channel_index = (channel_index + 1) % len(channel_ids)
                await asyncio.sleep(0.2)

            exhausted = not channel_ids
            if failed_channels > 0 and event_sink:
                breakdown = self._format_fetch_error_breakdown(dict(fetch_error_breakdown))
                event_sink(
                    {
                        "type": "log",
                        "level": "SKIP",
                        "message": f"Skipped {failed_channels} channels due to fetch errors ({breakdown})",
                    }
                )
            if exhausted:
                self.store.delete_setting(cursor_key)
            else:
                self.store.save_setting(
                    cursor_key,
                    {
                        "version": 1,
                        "channel_ids": channel_ids,
                        "channel_index": channel_index,
                        "before_by_channel": before_by_channel,
                    },
                )

        return PrepareResult(
            queued=queued,
            skipped=skipped,
            already_referenced=already_referenced,
            failed_channels=failed_channels,
            fetch_error_breakdown=dict(fetch_error_breakdown),
            exhausted=exhausted,
        )

    @staticmethod
    def _scrape_cursor_key(*, guild_id: str, mode: str, order_direction: str) -> str:
        return f"scrape_cursor:{guild_id}:{mode}:{order_direction}"

    @staticmethod
    def _message_priority(message_id: str) -> int | None:
        try:
            return int(message_id)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _format_fetch_error_breakdown(breakdown: dict[str, int]) -> str:
        if not breakdown:
            return "none"

        def _sort_key(item: tuple[str, int]) -> tuple[int, str]:
            status, _count = item
            try:
                return (0, f"{int(status):04d}")
            except ValueError:
                return (1, status)

        return ", ".join(f"{status}={count}" for status, count in sorted(breakdown.items(), key=_sort_key))

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
