"""Discord REST client with user-token auth and rate-limit handling."""

from __future__ import annotations

import asyncio
import logging
import random
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
        retry_after_seconds: float | None = None,
    ) -> None:
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class DiscordClient:
    def __init__(
        self,
        token: str,
        timeout_seconds: float = 20.0,
        min_request_gap_seconds: float = 0.4,
    ) -> None:
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._min_request_gap_seconds = min_request_gap_seconds
        self._client: httpx.AsyncClient | None = None
        self._global_block_until: float = 0.0
        self._route_to_bucket: dict[str, str] = {}
        self._bucket_block_until: dict[str, float] = {}
        self._next_request_not_before: float = 0.0

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

        transient_retries = 8
        route_key = f"{method.upper()}:{path}"
        last_retry_after: float | None = None
        for attempt in range(1, transient_retries + 1):
            await self._wait_for_rate_limit(route_key)
            try:
                response = await self._client.request(method, path, **kwargs)
                self._next_request_not_before = self._now() + self._min_request_gap_seconds
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
                retry_after = self._retry_after_seconds(response)
                last_retry_after = retry_after
                self._apply_429_block(route_key, response, retry_after)
                if attempt >= transient_retries:
                    raise DiscordApiError(
                        "Discord API rate limit did not recover",
                        status_code=429,
                        retryable=True,
                        retry_after_seconds=retry_after,
                    )
                await asyncio.sleep(max(retry_after, 0.5))
                continue

            if response.status_code in {500, 502, 503, 504} and attempt < transient_retries:
                await asyncio.sleep(min(2**attempt, 12))
                continue

            self._apply_success_headers(route_key, response)

            return response

        raise DiscordApiError(
            "Discord API did not recover after retries",
            retryable=True,
            retry_after_seconds=last_retry_after,
        )

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
            retry_after_seconds=self._retry_after_seconds(response) if response.status_code == 429 else None,
        )

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    @staticmethod
    def _to_float(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _retry_after_seconds(self, response: httpx.Response) -> float:
        payload_retry_after: float | None = None
        try:
            payload = response.json()
            payload_retry_after = self._to_float(str(payload.get("retry_after")))
        except Exception:
            payload_retry_after = None
        header_retry_after = self._to_float(response.headers.get("Retry-After"))
        result = payload_retry_after if payload_retry_after is not None else header_retry_after
        if result is None:
            return 2.0
        return max(result, 0.0)

    async def _wait_for_rate_limit(self, route_key: str) -> None:
        bucket_wait = 0.0
        bucket_id = self._route_to_bucket.get(route_key)
        now = self._now()
        if bucket_id:
            bucket_wait = max(0.0, self._bucket_block_until.get(bucket_id, 0.0) - now)

        global_wait = max(0.0, self._global_block_until - now)
        floor_wait = max(0.0, self._next_request_not_before - now)
        wait_for = max(bucket_wait, global_wait, floor_wait)
        if wait_for > 0:
            await asyncio.sleep(wait_for)

    def _apply_success_headers(self, route_key: str, response: httpx.Response) -> None:
        bucket_id = response.headers.get("X-RateLimit-Bucket")
        if bucket_id:
            self._route_to_bucket[route_key] = bucket_id

        remaining = response.headers.get("X-RateLimit-Remaining")
        reset_after = self._to_float(response.headers.get("X-RateLimit-Reset-After"))
        if bucket_id and remaining == "0" and reset_after and reset_after > 0:
            jitter = random.uniform(0.02, 0.15)
            self._bucket_block_until[bucket_id] = self._now() + reset_after + jitter

    def _apply_429_block(self, route_key: str, response: httpx.Response, retry_after: float) -> None:
        is_global = False
        try:
            payload = response.json()
            is_global = bool(payload.get("global", False))
        except Exception:
            is_global = False

        jitter = random.uniform(0.05, 0.25)
        block_until = self._now() + retry_after + jitter
        bucket_id = response.headers.get("X-RateLimit-Bucket") or self._route_to_bucket.get(route_key)

        if bucket_id:
            self._route_to_bucket[route_key] = bucket_id
            self._bucket_block_until[bucket_id] = max(self._bucket_block_until.get(bucket_id, 0.0), block_until)

        if is_global:
            self._global_block_until = max(self._global_block_until, block_until)

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
