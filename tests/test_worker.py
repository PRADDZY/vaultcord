import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from vaultcord.discord_api import DiscordApiError
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
        priority=None,
    )

    events = asyncio.run(_run_worker(store))
    progress = store.get_progress(guild_id="g1", mode="all")
    assert progress["done"] == 1
    assert any(event.get("type") == "log" for event in events)
    assert any(event.get("type") == "completed" for event in events)


def test_worker_non_retryable_error_fails_terminally(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    store.insert_archived_message(
        vault_id="id2",
        discord_message_id="m2",
        channel_id="c1",
        guild_id="g1",
        author_id="u1",
        mode="all",
        reference_text="vault://id2",
        encrypted_payload={"ciphertext_b64": "a", "nonce_b64": "b", "salt_b64": "c"},
    )
    store.enqueue_job(
        discord_message_id="m2",
        channel_id="c1",
        guild_id="g1",
        mode="all",
        vault_id="id2",
        priority=None,
    )

    session = VaultSession(user_id="u1", username="u", token="tok", password="pw")
    worker = ScrubWorker(
        store=store,
        session=session,
        scheduler=SchedulerConfig(
            edit_delay_min_seconds=0,
            edit_delay_max_seconds=0,
            run_hours_min=1,
            run_hours_max=1,
            pause_hours_min=0,
            pause_hours_max=0,
        ),
        request_timeout_seconds=5,
        max_retries=3,
    )
    control = WorkerControl()
    events: list[dict] = []

    async def _run() -> None:
        with patch("vaultcord.worker.DiscordClient") as client_cls, patch(
            "vaultcord.worker.apply_vault_reference",
            new=AsyncMock(side_effect=DiscordApiError("forbidden", status_code=403, retryable=False)),
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

    asyncio.run(_run())
    progress = store.get_progress(guild_id="g1", mode="all", max_attempts=3)
    assert progress["failed"] == 1
    assert progress["retryable_failed"] == 0
    assert progress["remaining"] == 0
    assert any("failed permanently" in str(event.get("message", "")) for event in events if event.get("type") == "log")


def test_worker_retryable_429_uses_retry_after_delay(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    store.insert_archived_message(
        vault_id="id3",
        discord_message_id="m3",
        channel_id="c1",
        guild_id="g1",
        author_id="u1",
        mode="all",
        reference_text="vault://id3",
        encrypted_payload={"ciphertext_b64": "a", "nonce_b64": "b", "salt_b64": "c"},
    )
    store.enqueue_job(
        discord_message_id="m3",
        channel_id="c1",
        guild_id="g1",
        mode="all",
        vault_id="id3",
        priority=None,
    )

    session = VaultSession(user_id="u1", username="u", token="tok", password="pw")
    worker = ScrubWorker(
        store=store,
        session=session,
        scheduler=SchedulerConfig(
            edit_delay_min_seconds=0,
            edit_delay_max_seconds=0,
            run_hours_min=1,
            run_hours_max=1,
            pause_hours_min=0,
            pause_hours_max=0,
        ),
        request_timeout_seconds=5,
        max_retries=3,
    )
    control = WorkerControl()
    recorded_delay: dict[str, int] = {"value": 0}

    original_mark_failed = store.mark_job_failed

    def wrapped_mark_failed(job_id: int, *, attempts: int, delay_seconds: int, error_message: str) -> None:
        recorded_delay["value"] = delay_seconds
        control.stop_event.set()
        original_mark_failed(job_id, attempts=attempts, delay_seconds=delay_seconds, error_message=error_message)

    async def _run() -> None:
        with patch("vaultcord.worker.DiscordClient") as client_cls, patch(
            "vaultcord.worker.apply_vault_reference",
            new=AsyncMock(
                side_effect=DiscordApiError(
                    "rate limited",
                    status_code=429,
                    retryable=True,
                    retry_after_seconds=65.0,
                )
            ),
        ), patch.object(store, "mark_job_failed", side_effect=wrapped_mark_failed):
            client_cls.return_value.__aenter__ = AsyncMock(return_value=client_cls.return_value)
            client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            await worker.run(
                guild_id="g1",
                mode="all",
                retry_failed_only=False,
                control=control,
                event_sink=lambda _event: None,
            )

    asyncio.run(_run())
    assert recorded_delay["value"] >= 66


def test_worker_refills_queue_via_callback(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    session = VaultSession(user_id="u1", username="u", token="tok", password="pw")
    worker = ScrubWorker(
        store=store,
        session=session,
        scheduler=SchedulerConfig(
            edit_delay_min_seconds=0,
            edit_delay_max_seconds=0,
            run_hours_min=1,
            run_hours_max=1,
            pause_hours_min=0,
            pause_hours_max=0,
        ),
        request_timeout_seconds=5,
        max_retries=3,
    )
    control = WorkerControl()
    calls = {"count": 0}

    async def refill_once() -> bool:
        calls["count"] += 1
        if calls["count"] > 1:
            return False
        inserted = store.insert_archived_message(
            vault_id="id-refill",
            discord_message_id="m-refill",
            channel_id="c1",
            guild_id="g1",
            author_id="u1",
            mode="all",
            reference_text="vault://id-refill",
            encrypted_payload={"ciphertext_b64": "a", "nonce_b64": "b", "salt_b64": "c"},
        )
        assert inserted
        enqueued = store.enqueue_job(
            discord_message_id="m-refill",
            channel_id="c1",
            guild_id="g1",
            mode="all",
            vault_id="id-refill",
            priority=None,
        )
        assert enqueued
        return True

    async def _run() -> None:
        with patch("vaultcord.worker.DiscordClient") as client_cls, patch(
            "vaultcord.worker.apply_vault_reference",
            new=AsyncMock(return_value=None),
        ):
            client_cls.return_value.__aenter__ = AsyncMock(return_value=client_cls.return_value)
            client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            await worker.run(
                guild_id="g1",
                mode="all",
                retry_failed_only=False,
                control=control,
                event_sink=lambda _event: None,
                queue_refill=refill_once,
            )

    asyncio.run(_run())
    progress = store.get_progress(guild_id="g1", mode="all")
    assert progress["done"] == 1
    assert calls["count"] >= 2
