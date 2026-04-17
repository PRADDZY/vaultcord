"""SQLite persistence for VaultCord queues and vault records."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .constants import STATUS_DONE, STATUS_FAILED, STATUS_PENDING
from .models import QueueJob


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class VaultStore:
    """Database wrapper with queue and encrypted-message storage."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    vault_id TEXT PRIMARY KEY,
                    discord_message_id TEXT UNIQUE NOT NULL,
                    channel_id TEXT NOT NULL,
                    guild_id TEXT,
                    author_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    reference_text TEXT NOT NULL,
                    encrypted_payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_message_id TEXT UNIQUE NOT NULL,
                    channel_id TEXT NOT NULL,
                    guild_id TEXT,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    vault_id TEXT NOT NULL,
                    leased_until TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(vault_id) REFERENCES messages(vault_id)
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status_next_attempt
                ON jobs(status, next_attempt_at);
                """
            )

    def save_setting(self, key: str, value: dict[str, Any]) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value), now),
            )

    def read_setting(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return json.loads(str(row["value"]))

    def vault_exists_for_message(self, discord_message_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM messages WHERE discord_message_id = ? LIMIT 1",
                (discord_message_id,),
            ).fetchone()
        return bool(row)

    def insert_archived_message(
        self,
        *,
        vault_id: str,
        discord_message_id: str,
        channel_id: str,
        guild_id: str | None,
        author_id: str,
        mode: str,
        reference_text: str,
        encrypted_payload: dict[str, Any],
    ) -> bool:
        now = utc_now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO messages(
                    vault_id, discord_message_id, channel_id, guild_id, author_id,
                    mode, reference_text, encrypted_payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    vault_id,
                    discord_message_id,
                    channel_id,
                    guild_id,
                    author_id,
                    mode,
                    reference_text,
                    json.dumps(encrypted_payload),
                    now,
                ),
            )
        return cursor.rowcount == 1

    def enqueue_job(
        self,
        *,
        discord_message_id: str,
        channel_id: str,
        guild_id: str | None,
        mode: str,
        vault_id: str,
        status: str = STATUS_PENDING,
    ) -> bool:
        now = utc_now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO jobs(
                    discord_message_id, channel_id, guild_id, mode, status,
                    attempts, next_attempt_at, last_error, vault_id,
                    leased_until, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, NULL, ?, NULL, ?, ?)
                """,
                (
                    discord_message_id,
                    channel_id,
                    guild_id,
                    mode,
                    status,
                    now,
                    vault_id,
                    now,
                    now,
                ),
            )
        return cursor.rowcount == 1

    def claim_next_job(self, *, max_attempts: int, retry_failed_only: bool = False) -> QueueJob | None:
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        stale_lease_iso = now.isoformat()
        with self._connect() as conn:
            if retry_failed_only:
                where = "status = ?"
                params: list[Any] = [STATUS_FAILED]
            else:
                where = "status IN (?, ?)"
                params = [STATUS_PENDING, STATUS_FAILED]

            row = conn.execute(
                f"""
                SELECT * FROM jobs
                WHERE {where}
                  AND attempts < ?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  AND (leased_until IS NULL OR leased_until <= ?)
                ORDER BY updated_at ASC
                LIMIT 1
                """,
                (*params, max_attempts, now_iso, stale_lease_iso),
            ).fetchone()

            if not row:
                return None

            lease_until = (now + timedelta(minutes=2)).isoformat()
            conn.execute(
                "UPDATE jobs SET leased_until = ?, updated_at = ? WHERE id = ?",
                (lease_until, now_iso, row["id"]),
            )

        return QueueJob(
            id=int(row["id"]),
            discord_message_id=str(row["discord_message_id"]),
            channel_id=str(row["channel_id"]),
            guild_id=str(row["guild_id"]) if row["guild_id"] is not None else None,
            mode=str(row["mode"]),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            next_attempt_at=str(row["next_attempt_at"]) if row["next_attempt_at"] else None,
            last_error=str(row["last_error"]) if row["last_error"] else None,
            vault_id=str(row["vault_id"]),
        )

    def mark_job_done(self, job_id: int) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, leased_until = NULL, next_attempt_at = NULL,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (STATUS_DONE, now, job_id),
            )

    def mark_job_failed(self, job_id: int, *, attempts: int, delay_seconds: int, error_message: str) -> None:
        now = datetime.now(UTC)
        next_attempt = (now + timedelta(seconds=delay_seconds)).isoformat()
        sanitized_error = error_message[:180]
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, attempts = ?, next_attempt_at = ?,
                    last_error = ?, leased_until = NULL, updated_at = ?
                WHERE id = ?
                """,
                (STATUS_FAILED, attempts, next_attempt, sanitized_error, now.isoformat(), job_id),
            )

    def release_job_lease(self, job_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET leased_until = NULL, updated_at = ? WHERE id = ?",
                (utc_now_iso(), job_id),
            )

    def reset_failed_jobs(self, *, guild_id: str | None, mode: str | None) -> int:
        where = ["status = ?"]
        params: list[Any] = [STATUS_FAILED]
        if guild_id:
            where.append("guild_id = ?")
            params.append(guild_id)
        if mode:
            where.append("mode = ?")
            params.append(mode)

        query = f"UPDATE jobs SET status = ?, next_attempt_at = ?, updated_at = ? WHERE {' AND '.join(where)}"
        now = utc_now_iso()
        with self._connect() as conn:
            cursor = conn.execute(query, (STATUS_PENDING, now, now, *params))
        return int(cursor.rowcount)

    def get_progress(self, *, guild_id: str | None = None, mode: str | None = None) -> dict[str, int]:
        where = []
        params: list[Any] = []
        if guild_id:
            where.append("guild_id = ?")
            params.append(guild_id)
        if mode:
            where.append("mode = ?")
            params.append(mode)

        suffix = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) AS c FROM jobs {suffix}", params).fetchone()["c"]
            done = conn.execute(
                f"SELECT COUNT(*) AS c FROM jobs {suffix} {'AND' if suffix else 'WHERE'} status = ?",
                (*params, STATUS_DONE),
            ).fetchone()["c"]
            failed = conn.execute(
                f"SELECT COUNT(*) AS c FROM jobs {suffix} {'AND' if suffix else 'WHERE'} status = ?",
                (*params, STATUS_FAILED),
            ).fetchone()["c"]
        return {
            "total": int(total),
            "done": int(done),
            "failed": int(failed),
            "remaining": max(int(total) - int(done) - int(failed), 0),
        }

    def get_encrypted_message(self, vault_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT encrypted_payload FROM messages WHERE vault_id = ? LIMIT 1",
                (vault_id,),
            ).fetchone()
        if not row:
            return None
        return json.loads(str(row["encrypted_payload"]))

    def find_vault_id_for_message(self, discord_message_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT vault_id FROM messages WHERE discord_message_id = ? LIMIT 1",
                (discord_message_id,),
            ).fetchone()
        if not row:
            return None
        return str(row["vault_id"])

    def release_all_leases(self) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE jobs SET leased_until = NULL, updated_at = ?", (utc_now_iso(),))
