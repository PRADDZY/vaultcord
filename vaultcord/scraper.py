"""Guild-wide message scraping and mode filtering."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable
from typing import Any

from .constants import CHANNEL_TEXT_TYPES, CHANNEL_THREAD_TYPES, MODE_ALL, MODE_LINKS, MODE_MEDIA, MODE_TEXT
from .discord_api import DiscordClient, DiscordApiError
from .models import ScrapedMessage

LOGGER = logging.getLogger(__name__)
_LINK_RE = re.compile(r"https?://", flags=re.IGNORECASE)


class MessageScraper:
    def __init__(self, client: DiscordClient, user_id: str) -> None:
        self.client = client
        self.user_id = user_id

    def detect_mode(self, message: dict[str, Any]) -> str:
        content = str(message.get("content") or "")
        attachments = message.get("attachments") or []
        if attachments:
            return MODE_MEDIA
        if _LINK_RE.search(content):
            return MODE_LINKS
        return MODE_TEXT

    def mode_matches(self, message_mode: str, selected_mode: str) -> bool:
        if selected_mode == MODE_ALL:
            return True
        return message_mode == selected_mode

    async def discover_channel_ids(self, guild_id: str) -> list[str]:
        channels = await self.client.list_guild_channels(guild_id)
        parent_channels = [c for c in channels if int(c.get("type", -1)) in CHANNEL_TEXT_TYPES]

        channel_ids: set[str] = {str(c["id"]) for c in parent_channels}

        threads = await self.client.list_active_threads(guild_id)
        for thread in threads:
            if int(thread.get("type", -1)) in CHANNEL_THREAD_TYPES:
                channel_ids.add(str(thread["id"]))

        for channel in parent_channels:
            try:
                archived = await self.client.list_archived_threads(str(channel["id"]))
            except DiscordApiError:
                continue
            for thread in archived:
                if int(thread.get("type", -1)) in CHANNEL_THREAD_TYPES:
                    channel_ids.add(str(thread["id"]))
            await asyncio.sleep(0.1)

        return sorted(channel_ids)

    async def iter_user_messages(
        self,
        guild_id: str,
        mode: str,
        *,
        on_channel_progress: Callable[[str], None] | None = None,
    ) -> AsyncIterator[ScrapedMessage]:
        channel_ids = await self.discover_channel_ids(guild_id)
        LOGGER.info("Discovered %s channels/threads for guild %s", len(channel_ids), guild_id)

        for channel_id in channel_ids:
            if on_channel_progress:
                on_channel_progress(channel_id)

            before: str | None = None
            while True:
                try:
                    batch = await self.client.fetch_channel_messages(channel_id, before=before, limit=100)
                except DiscordApiError as exc:
                    LOGGER.warning("Skipping channel %s after API error: %s", channel_id, exc)
                    break

                if not batch:
                    break

                for message in batch:
                    author_id = str((message.get("author") or {}).get("id") or "")
                    if author_id != self.user_id:
                        continue

                    message_mode = self.detect_mode(message)
                    if not self.mode_matches(message_mode, mode):
                        continue

                    yield ScrapedMessage(
                        message_id=str(message["id"]),
                        channel_id=str(channel_id),
                        guild_id=guild_id,
                        author_id=author_id,
                        content=str(message.get("content") or ""),
                        attachments=list(message.get("attachments") or []),
                        timestamp=str(message.get("timestamp") or ""),
                        channel_type=message.get("type"),
                    )

                before = str(batch[-1]["id"])
                await asyncio.sleep(0.2)
