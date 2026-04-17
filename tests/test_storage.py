from pathlib import Path

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
        status=STATUS_FAILED,
    )

    job = store.claim_next_job(max_attempts=3, retry_failed_only=True)
    assert job is not None
    store.mark_job_failed(job.id, attempts=3, delay_seconds=5, error_message="bad")

    progress_before = store.get_progress(guild_id="g1", mode="links")
    assert progress_before["failed"] == 1

    reset = store.reset_failed_jobs(guild_id="g1", mode="links")
    assert reset == 1

    claimed = store.claim_next_job(max_attempts=3)
    assert claimed is not None
    store.mark_job_done(claimed.id)
    progress_after = store.get_progress(guild_id="g1", mode="links")
    assert progress_after["done"] == 1

    assert STATUS_DONE == "done"
    assert STATUS_FAILED == "failed"
