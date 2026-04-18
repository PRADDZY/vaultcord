import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from vaultcord.models import AppConfig, SchedulerConfig, VaultSession
from vaultcord.service import VaultService
from vaultcord.storage import VaultStore


def _message_payload(message_id: int, *, author_id: str = "u1", content: str = "hello world") -> dict[str, object]:
    return {
        "id": str(message_id),
        "channel_id": "c1",
        "guild_id": "g1",
        "author": {"id": author_id},
        "content": content,
        "attachments": [],
        "timestamp": "2026-01-01T00:00:00Z",
    }


def build_service(tmp_path: Path) -> VaultService:
    config = AppConfig(
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "vault.db"),
        log_path=str(tmp_path / "vault.log"),
        request_timeout_seconds=20,
        max_retries=3,
        scheduler=SchedulerConfig(),
    )
    store = VaultStore(config.db_path)
    return VaultService(config=config, store=store)


def test_prepare_jobs_archives_and_enqueues(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    session = VaultSession(user_id="u1", username="user#0001", token="t", password="pw")

    with patch("vaultcord.service.DiscordClient") as client_cls, patch(
        "vaultcord.service.MessageScraper.discover_channel_ids", new=AsyncMock(return_value=["c1"])
    ):
        client_cls.return_value.__aenter__ = AsyncMock(return_value=client_cls.return_value)
        client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        client_cls.return_value.fetch_channel_messages = AsyncMock(
            side_effect=[
                [_message_payload(101)],
                [],
            ]
        )

        result = asyncio.run(service.prepare_jobs(session, guild_id="g1", mode="all"))

    assert result.queued == 1
    progress = service.progress(guild_id="g1", mode="all")
    assert progress["total"] == 1
    assert progress["remaining"] == 1


def test_decrypt_vault_message(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    session = VaultSession(user_id="u1", username="user#0001", token="t", password="pw")

    with patch("vaultcord.service.DiscordClient") as client_cls, patch(
        "vaultcord.service.MessageScraper.discover_channel_ids", new=AsyncMock(return_value=["c1"])
    ):
        client_cls.return_value.__aenter__ = AsyncMock(return_value=client_cls.return_value)
        client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        client_cls.return_value.fetch_channel_messages = AsyncMock(
            side_effect=[
                [_message_payload(102)],
                [],
            ]
        )

        asyncio.run(service.prepare_jobs(session, guild_id="g1", mode="all"))

    vault_id = service.store.find_vault_id_for_message("102")
    assert vault_id is not None
    data = service.decrypt_vault_message(vault_id=vault_id, password="pw")
    assert data["content"] == "hello world"


def test_has_retryable_queue_detects_existing_work(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    inserted = service.store.insert_archived_message(
        vault_id="id-queue",
        discord_message_id="m-queue",
        channel_id="c1",
        guild_id="g1",
        author_id="u1",
        mode="all",
        reference_text="vault://id-queue",
        encrypted_payload={"ciphertext_b64": "a", "nonce_b64": "b", "salt_b64": "c"},
    )
    assert inserted
    enqueued = service.store.enqueue_job(
        discord_message_id="m-queue",
        channel_id="c1",
        guild_id="g1",
        mode="all",
        vault_id="id-queue",
        priority=None,
    )
    assert enqueued
    assert service.has_retryable_queue(guild_id="g1", mode="all")


def test_prepare_jobs_batch_resumes_from_cursor(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    session = VaultSession(user_id="u1", username="user#0001", token="t", password="pw")
    messages = [_message_payload(mid) for mid in range(200, 190, -1)]

    async def _fetch(_channel_id: str, *, before: str | None = None, limit: int = 100) -> list[dict[str, object]]:
        if before is None:
            candidates = messages
        else:
            before_value = int(before)
            candidates = [m for m in messages if int(str(m["id"])) < before_value]
        return candidates[: min(limit, 5)]

    with patch("vaultcord.service.DiscordClient") as client_cls, patch(
        "vaultcord.service.MessageScraper.discover_channel_ids", new=AsyncMock(return_value=["c1"])
    ):
        client_cls.return_value.__aenter__ = AsyncMock(return_value=client_cls.return_value)
        client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        client_cls.return_value.fetch_channel_messages = AsyncMock(side_effect=_fetch)

        first = asyncio.run(
            service.prepare_jobs_batch(
                session,
                guild_id="g1",
                mode="all",
                order_direction="newest",
                batch_size=3,
            )
        )
        second = asyncio.run(
            service.prepare_jobs_batch(
                session,
                guild_id="g1",
                mode="all",
                order_direction="newest",
                batch_size=3,
            )
        )
        final_batch = asyncio.run(
            service.prepare_jobs_batch(
                session,
                guild_id="g1",
                mode="all",
                order_direction="newest",
                batch_size=20,
            )
        )

    assert first.queued == 3
    assert not first.exhausted
    assert second.queued == 3
    assert final_batch.exhausted
    progress = service.progress(guild_id="g1", mode="all")
    assert progress["total"] == 10
    cursor_key = service._scrape_cursor_key(guild_id="g1", mode="all", order_direction="newest")
    assert service.store.read_setting(cursor_key) is None
