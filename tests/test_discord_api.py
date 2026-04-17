import asyncio

import pytest

from vaultcord.discord_api import DiscordApiError, DiscordClient


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


async def _run_list_archived_threads() -> list[dict]:
    client = DiscordClient(token="x")

    pages = {
        ("/channels/c1/threads/archived/public", None): FakeResponse(
            200,
            {
                "threads": [
                    {"id": "t1", "thread_metadata": {"archive_timestamp": "2026-01-03T00:00:00.000000+00:00"}},
                    {"id": "t2", "thread_metadata": {"archive_timestamp": "2026-01-02T00:00:00.000000+00:00"}},
                ],
                "has_more": True,
            },
        ),
        (
            "/channels/c1/threads/archived/public",
            "2026-01-02T00:00:00.000000+00:00",
        ): FakeResponse(
            200,
            {
                "threads": [
                    {"id": "t3", "thread_metadata": {"archive_timestamp": "2026-01-01T00:00:00.000000+00:00"}},
                ],
                "has_more": False,
            },
        ),
        ("/channels/c1/users/@me/threads/archived/private", None): FakeResponse(
            200,
            {
                "threads": [
                    {"id": "t3", "thread_metadata": {"archive_timestamp": "2026-01-01T00:00:00.000000+00:00"}},
                    {"id": "t4", "thread_metadata": {"archive_timestamp": "2026-01-01T00:00:00.000000+00:00"}},
                ],
                "has_more": False,
            },
        ),
    }

    async def fake_request(method: str, path: str, **kwargs):
        assert method == "GET"
        before = (kwargs.get("params") or {}).get("before")
        return pages[(path, before)]

    client._request = fake_request  # type: ignore[method-assign]
    return await client.list_archived_threads("c1")


def test_list_archived_threads_paginates_and_deduplicates() -> None:
    threads = asyncio.run(_run_list_archived_threads())
    ids = [thread["id"] for thread in threads]
    assert ids == ["t1", "t2", "t3", "t4"]


def test_retryable_status_classification() -> None:
    client = DiscordClient(token="x")
    assert client._is_retryable_status(500)
    assert client._is_retryable_status(409)
    assert not client._is_retryable_status(403)


def test_expect_status_marks_retryable_flag() -> None:
    client = DiscordClient(token="x")
    with pytest.raises(DiscordApiError) as exc_info:
        client._expect_status(FakeResponse(503, {}), ok_statuses={200}, message="bad")
    assert exc_info.value.retryable
