"""Discord REST client with user-token auth and rate-limit handling."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .constants import DISCORD_API_BASE

LOGGER = logging.getLogger(__name__)


class DiscordApiError(RuntimeError):
    """Raised for API failures with sanitized context."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(message)


class DiscordClient:
    def __init__(self, token: str, timeout_seconds: float = 20.0) -> None:
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "DiscordClient":
        self._client = httpx.AsyncClient(
            base_url=DISCORD_API_BASE,
            timeout=self._timeout_seconds,
            headers={
                "Authorization": self._token,
                "User-Agent": "VaultCord/0.1 (+local)",
                "Content-Type": "application/json",
            },
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("DiscordClient is not started")

        transient_retries = 4
        for attempt in range(1, transient_retries + 1):
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.TimeoutException:
                if attempt < transient_retries:
                    await asyncio.sleep(min(2**attempt, 12))
                    continue
                raise DiscordApiError("Discord API timeout", retryable=True) from None
            except httpx.TransportError:
                if attempt < transient_retries:
                    await asyncio.sleep(min(2**attempt, 12))
                    continue
                raise DiscordApiError("Discord API transport failure", retryable=True) from None

            if response.status_code == 429:
                retry_after = float(response.json().get("retry_after", 2.0))
                await asyncio.sleep(max(retry_after, 0.5))
                continue

            if response.status_code in {500, 502, 503, 504} and attempt < transient_retries:
                await asyncio.sleep(min(2**attempt, 12))
                continue

            reset_after = response.headers.get("X-RateLimit-Reset-After")
            remaining = response.headers.get("X-RateLimit-Remaining")
            if remaining == "0" and reset_after:
                try:
                    sleep_seconds = float(reset_after)
                    if sleep_seconds > 0:
                        await asyncio.sleep(sleep_seconds)
                except ValueError:
                    pass

            return response

        raise DiscordApiError("Discord API did not recover after retries")

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code in {408, 409, 425, 429} or status_code >= 500

    def _expect_status(
        self,
        response: httpx.Response,
        *,
        ok_statuses: set[int],
        message: str,
    ) -> None:
        if response.status_code in ok_statuses:
            return
        raise DiscordApiError(
            message,
            status_code=response.status_code,
            retryable=self._is_retryable_status(response.status_code),
        )

    async def get_me(self) -> dict[str, Any]:
        response = await self._request("GET", "/users/@me")
        self._expect_status(response, ok_statuses={200}, message="Token validation failed")
        return response.json()

    async def list_guild_channels(self, guild_id: str) -> list[dict[str, Any]]:
        response = await self._request("GET", f"/guilds/{guild_id}/channels")
        self._expect_status(response, ok_statuses={200}, message="Failed to list guild channels")
        return response.json()

    async def list_active_threads(self, guild_id: str) -> list[dict[str, Any]]:
        response = await self._request("GET", f"/guilds/{guild_id}/threads/active")
        if response.status_code != 200:
            LOGGER.warning("Active thread listing failed for guild %s", guild_id)
            return []
        data = response.json()
        return data.get("threads", [])

    async def list_archived_threads(self, parent_channel_id: str) -> list[dict[str, Any]]:
        threads: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        async def _collect(path: str) -> None:
            before: str | None = None
            while True:
                params: dict[str, Any] = {"limit": 100}
                if before:
                    params["before"] = before
                response = await self._request("GET", path, params=params)
                if response.status_code != 200:
                    return
                payload = response.json()
                page_threads = payload.get("threads", [])
                if not page_threads:
                    return

                for thread in page_threads:
                    thread_id = str(thread.get("id", ""))
                    if thread_id and thread_id not in seen_ids:
                        seen_ids.add(thread_id)
                        threads.append(thread)

                if not payload.get("has_more"):
                    return

                thread_metadata = page_threads[-1].get("thread_metadata") or {}
                before = thread_metadata.get("archive_timestamp")
                if not before:
                    return

        await _collect(f"/channels/{parent_channel_id}/threads/archived/public")
        await _collect(f"/channels/{parent_channel_id}/users/@me/threads/archived/private")
        return threads

    async def fetch_channel_messages(
        self,
        channel_id: str,
        *,
        before: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if before:
            params["before"] = before
        response = await self._request("GET", f"/channels/{channel_id}/messages", params=params)
        self._expect_status(response, ok_statuses={200}, message="Failed to fetch messages")
        return response.json()

    async def edit_message(self, channel_id: str, message_id: str, content: str) -> dict[str, Any]:
        response = await self._request(
            "PATCH",
            f"/channels/{channel_id}/messages/{message_id}",
            json={"content": content},
        )
        self._expect_status(response, ok_statuses={200, 202}, message="Failed to edit message")
        return response.json()
