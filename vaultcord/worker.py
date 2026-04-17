"""Long-running queue worker for Discord message replacement."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any, Callable

from .discord_api import DiscordApiError, DiscordClient
from .editor import apply_vault_reference
from .models import SchedulerConfig, VaultSession
from .storage import VaultStore

LOGGER = logging.getLogger(__name__)

EventSink = Callable[[dict[str, Any]], None]


@dataclass(slots=True)
class WorkerControl:
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)


class ScrubWorker:
    def __init__(
        self,
        *,
        store: VaultStore,
        session: VaultSession,
        scheduler: SchedulerConfig,
        request_timeout_seconds: float,
        max_retries: int,
    ) -> None:
        self.store = store
        self.session = session
        self.scheduler = scheduler
        self.request_timeout_seconds = request_timeout_seconds
        self.max_retries = max_retries

    async def run(
        self,
        *,
        guild_id: str | None,
        mode: str | None,
        retry_failed_only: bool,
        control: WorkerControl,
        event_sink: EventSink,
    ) -> None:
        start = perf_counter()
        session_deadline = datetime.now(UTC) + timedelta(hours=self._random_run_hours())
        event_sink({"type": "status", "status": "running"})

        async with DiscordClient(token=self.session.token, timeout_seconds=self.request_timeout_seconds) as client:
            while not control.stop_event.is_set():
                if control.pause_event.is_set():
                    event_sink({"type": "status", "status": "paused"})
                    await self._wait_until_resumed_or_stopped(control)
                    event_sink({"type": "status", "status": "running"})

                if datetime.now(UTC) >= session_deadline:
                    pause_hours = self._random_pause_hours()
                    pause_seconds = int(pause_hours * 3600)
                    event_sink({
                        "type": "log",
                        "level": "SKIP",
                        "message": f"Session pause for {pause_hours:.2f}h to reduce API pressure",
                    })
                    event_sink({"type": "status", "status": "paused"})
                    await self._sleep_with_stop(pause_seconds, control)
                    session_deadline = datetime.now(UTC) + timedelta(hours=self._random_run_hours())
                    event_sink({"type": "status", "status": "running"})

                job = self.store.claim_next_job(
                    max_attempts=self.max_retries,
                    retry_failed_only=retry_failed_only,
                )
                if not job:
                    progress = self.store.get_progress(guild_id=guild_id, mode=mode)
                    event_sink({"type": "progress", **progress, "elapsed_seconds": int(perf_counter() - start)})
                    if progress["remaining"] == 0:
                        break
                    await self._sleep_with_stop(5, control)
                    continue

                try:
                    await apply_vault_reference(
                        client,
                        channel_id=job.channel_id,
                        message_id=job.discord_message_id,
                        vault_id=job.vault_id,
                    )
                    self.store.mark_job_done(job.id)
                    event_sink({
                        "type": "log",
                        "level": "OK",
                        "message": f"Updated message {job.discord_message_id}",
                    })
                except DiscordApiError as exc:
                    attempts = job.attempts + 1
                    retry_delay = min(30 * attempts, 300)
                    self.store.mark_job_failed(
                        job.id,
                        attempts=attempts,
                        delay_seconds=retry_delay,
                        error_message=f"status={exc.status_code}",
                    )
                    if attempts >= self.max_retries:
                        event_sink(
                            {
                                "type": "log",
                                "level": "FAIL",
                                "message": f"Message {job.discord_message_id} failed permanently",
                            }
                        )
                    else:
                        event_sink(
                            {
                                "type": "log",
                                "level": "FAIL",
                                "message": f"Message {job.discord_message_id} failed; retry {attempts}/{self.max_retries}",
                            }
                        )
                except Exception as exc:  # pragma: no cover - defensive guard
                    LOGGER.exception("Unexpected worker error")
                    attempts = job.attempts + 1
                    retry_delay = min(30 * attempts, 300)
                    self.store.mark_job_failed(
                        job.id,
                        attempts=attempts,
                        delay_seconds=retry_delay,
                        error_message="unexpected worker failure",
                    )
                    event_sink({
                        "type": "log",
                        "level": "FAIL",
                        "message": f"Message {job.discord_message_id} failed unexpectedly: {type(exc).__name__}",
                    })
                finally:
                    self.store.release_job_lease(job.id)

                progress = self.store.get_progress(guild_id=guild_id, mode=mode)
                event_sink({"type": "progress", **progress, "elapsed_seconds": int(perf_counter() - start)})
                delay = random.randint(
                    self.scheduler.edit_delay_min_seconds,
                    self.scheduler.edit_delay_max_seconds,
                )
                await self._sleep_with_stop(delay, control)

        event_sink({"type": "status", "status": "idle"})

    def _random_run_hours(self) -> float:
        return random.uniform(self.scheduler.run_hours_min, self.scheduler.run_hours_max)

    def _random_pause_hours(self) -> float:
        return random.uniform(self.scheduler.pause_hours_min, self.scheduler.pause_hours_max)

    async def _sleep_with_stop(self, seconds: int, control: WorkerControl) -> None:
        for _ in range(max(seconds, 1)):
            if control.stop_event.is_set():
                return
            await asyncio.sleep(1)

    async def _wait_until_resumed_or_stopped(self, control: WorkerControl) -> None:
        while control.pause_event.is_set() and not control.stop_event.is_set():
            await asyncio.sleep(0.2)
