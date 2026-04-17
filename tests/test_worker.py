import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from vaultcord.models import SchedulerConfig, VaultSession
from vaultcord.storage import VaultStore
from vaultcord.worker import ScrubWorker, WorkerControl


def build_store(tmp_path: Path) -> VaultStore:
    return VaultStore(str(tmp_path / "worker.db"))


async def _run_worker(store: VaultStore) -> list[dict]:
    events: list[dict] = []
    session = VaultSession(user_id="u1", username="u", token="tok", password="pw")
    worker = ScrubWorker(
        store=store,
        session=session,
        scheduler=SchedulerConfig(
            edit_delay_min_seconds=0,
            edit_delay_max_seconds=0,
            run_hours_min=3,
            run_hours_max=3,
            pause_hours_min=0,
            pause_hours_max=0,
        ),
        request_timeout_seconds=5,
        max_retries=3,
    )

    control = WorkerControl()

    with patch("vaultcord.worker.DiscordClient") as client_cls, patch(
        "vaultcord.worker.apply_vault_reference", new=AsyncMock(return_value=None)
    ):
        client_cls.return_value.__aenter__ = AsyncMock(return_value=client_cls.return_value)
        client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        await worker.run(
            guild_id="g1",
            mode="all",
            retry_failed_only=False,
            control=control,
            event_sink=events.append,
        )

    return events


def test_worker_marks_jobs_done(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    store.insert_archived_message(
        vault_id="id1",
        discord_message_id="m1",
        channel_id="c1",
        guild_id="g1",
        author_id="u1",
        mode="all",
        reference_text="vault://id1",
        encrypted_payload={"ciphertext_b64": "a", "nonce_b64": "b", "salt_b64": "c"},
    )
    store.enqueue_job(
        discord_message_id="m1",
        channel_id="c1",
        guild_id="g1",
        mode="all",
        vault_id="id1",
    )

    events = asyncio.run(_run_worker(store))
    progress = store.get_progress(guild_id="g1", mode="all")
    assert progress["done"] == 1
    assert any(event.get("type") == "log" for event in events)
