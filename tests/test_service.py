import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from vaultcord.models import AppConfig, SchedulerConfig, ScrapedMessage, VaultSession
from vaultcord.service import VaultService
from vaultcord.storage import VaultStore


async def _yield_messages(*_: object, **__: object):
    yield ScrapedMessage(
        message_id="m1",
        channel_id="c1",
        guild_id="g1",
        author_id="u1",
        content="hello world",
        attachments=[],
        timestamp="2026-01-01T00:00:00Z",
    )


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
        "vaultcord.service.MessageScraper"
    ) as scraper_cls:
        client_cls.return_value.__aenter__ = AsyncMock(return_value=client_cls.return_value)
        client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        scraper = scraper_cls.return_value
        scraper.iter_user_messages = _yield_messages

        result = asyncio.run(service.prepare_jobs(session, guild_id="g1", mode="all"))

    assert result.queued == 1
    progress = service.progress(guild_id="g1", mode="all")
    assert progress["total"] == 1
    assert progress["remaining"] == 1


def test_decrypt_vault_message(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    session = VaultSession(user_id="u1", username="user#0001", token="t", password="pw")

    with patch("vaultcord.service.DiscordClient") as client_cls, patch(
        "vaultcord.service.MessageScraper"
    ) as scraper_cls:
        client_cls.return_value.__aenter__ = AsyncMock(return_value=client_cls.return_value)
        client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        scraper = scraper_cls.return_value
        scraper.iter_user_messages = _yield_messages

        asyncio.run(service.prepare_jobs(session, guild_id="g1", mode="all"))

    vault_id = service.store.find_vault_id_for_message("m1")
    assert vault_id is not None
    data = service.decrypt_vault_message(vault_id=vault_id, password="pw")
    assert data["content"] == "hello world"
