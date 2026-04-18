from pathlib import Path

from vaultcord.constants import ORDER_NEWEST, ORDER_OLDEST
from vaultcord.constants import STATUS_DONE, STATUS_FAILED
from vaultcord.storage import VaultStore


def build_store(tmp_path: Path) -> VaultStore:
    return VaultStore(str(tmp_path / "test.db"))


def test_insert_and_claim_job(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    inserted = store.insert_archived_message(
        vault_id="id1",
        discord_message_id="m1",
        channel_id="c1",
        guild_id="g1",
        author_id="u1",
        mode="all",
        reference_text="vault://id1",
        encrypted_payload={"ciphertext_b64": "a", "nonce_b64": "b", "salt_b64": "c"},
    )
    assert inserted

    enqueued = store.enqueue_job(
        discord_message_id="m1",
        channel_id="c1",
        guild_id="g1",
        mode="all",
        vault_id="id1",
        priority=None,
    )
    assert enqueued

    job = store.claim_next_job(max_attempts=3)
    assert job is not None
    assert job.discord_message_id == "m1"

    store.mark_job_done(job.id)
    progress = store.get_progress(guild_id="g1", mode="all")
    assert progress["done"] == 1


def test_failed_reset(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    store.insert_archived_message(
        vault_id="id2",
        discord_message_id="m2",
        channel_id="c1",
        guild_id="g1",
        author_id="u1",
        mode="links",
        reference_text="vault://id2",
        encrypted_payload={"ciphertext_b64": "a", "nonce_b64": "b", "salt_b64": "c"},
    )
    store.enqueue_job(
        discord_message_id="m2",
        channel_id="c1",
        guild_id="g1",
        mode="links",
        vault_id="id2",
        priority=None,
        status=STATUS_FAILED,
    )

    job = store.claim_next_job(max_attempts=3, retry_failed_only=True)
    assert job is not None
    store.mark_job_failed(job.id, attempts=3, delay_seconds=5, error_message="bad")

    progress_before = store.get_progress(guild_id="g1", mode="links", max_attempts=3)
    assert progress_before["failed"] == 1

    reset = store.reset_failed_jobs(guild_id="g1", mode="links")
    assert reset == 1

    claimed = store.claim_next_job(max_attempts=3)
    assert claimed is not None
    store.mark_job_done(claimed.id)
    progress_after = store.get_progress(guild_id="g1", mode="links", max_attempts=3)
    assert progress_after["done"] == 1

    assert STATUS_DONE == "done"
    assert STATUS_FAILED == "failed"


def test_retryable_failed_is_still_remaining(tmp_path: Path) -> None:
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

    job = store.claim_next_job(max_attempts=3)
    assert job is not None
    store.mark_job_failed(job.id, attempts=1, delay_seconds=30, error_message="temporary")

    progress = store.get_progress(guild_id="g1", mode="all", max_attempts=3)
    assert progress["failed"] == 0
    assert progress["retryable_failed"] == 1
    assert progress["remaining"] == 1
    assert store.has_retryable_work(guild_id="g1", mode="all", max_attempts=3)


def test_claim_next_job_respects_order_direction(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    for vault_id, msg_id in [("id4", "100"), ("id5", "200")]:
        store.insert_archived_message(
            vault_id=vault_id,
            discord_message_id=msg_id,
            channel_id="c1",
            guild_id="g1",
            author_id="u1",
            mode="all",
            reference_text=f"vault://{vault_id}",
            encrypted_payload={"ciphertext_b64": "a", "nonce_b64": "b", "salt_b64": "c"},
        )
        store.enqueue_job(
            discord_message_id=msg_id,
            channel_id="c1",
            guild_id="g1",
            mode="all",
            vault_id=vault_id,
            priority=int(msg_id),
        )

    newest = store.claim_next_job(max_attempts=3, order_direction=ORDER_NEWEST)
    assert newest is not None
    assert newest.discord_message_id == "200"

    store.release_job_lease(newest.id)
    store.mark_job_done(newest.id)

    oldest = store.claim_next_job(max_attempts=3, order_direction=ORDER_OLDEST)
    assert oldest is not None
    assert oldest.discord_message_id == "100"


def test_claim_next_job_respects_scope_filters(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    entries = [
        ("id-s1", "m-s1", "g1", "all"),
        ("id-s2", "m-s2", "g2", "links"),
    ]
    for vault_id, msg_id, guild_id, mode in entries:
        store.insert_archived_message(
            vault_id=vault_id,
            discord_message_id=msg_id,
            channel_id="c1",
            guild_id=guild_id,
            author_id="u1",
            mode=mode,
            reference_text=f"vault://{vault_id}",
            encrypted_payload={"ciphertext_b64": "a", "nonce_b64": "b", "salt_b64": "c"},
        )
        store.enqueue_job(
            discord_message_id=msg_id,
            channel_id="c1",
            guild_id=guild_id,
            mode=mode,
            vault_id=vault_id,
            priority=None,
        )

    scoped = store.claim_next_job(max_attempts=3, guild_id="g1", mode="all")
    assert scoped is not None
    assert scoped.discord_message_id == "m-s1"

    none_left = store.claim_next_job(max_attempts=3, guild_id="g1", mode="all")
    assert none_left is None
