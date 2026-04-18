"""Long-running queue worker for Discord message replacement."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import perf_counter
from collections.abc import Awaitable
from typing import Any, Callable

from .constants import ORDER_NEWEST
from .discord_api import DiscordApiError, DiscordClient
from .editor import apply_vault_reference
from .models import SchedulerConfig, VaultSession
from .sleep_inhibitor import SleepInhibitor
from .storage import VaultStore

LOGGER = logging.getLogger(__name__)

EventSink = Callable[[dict[str, Any]], None]
QueueRefill = Callable[[], Awaitable[bool]]


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
        order_direction: str = ORDER_NEWEST,
        control: WorkerControl,
        event_sink: EventSink,
        queue_refill: QueueRefill | None = None,
    ) -> None:
        start = perf_counter()
        session_deadline = datetime.now(UTC) + timedelta(hours=self._random_run_hours())
        completed = False
        initial_batch_preparing_logged = False
        event_sink({"type": "status", "status": "running"})
        inhibitor = SleepInhibitor()
        inhibited, detail = inhibitor.acquire()
        event_sink(
            {
                "type": "log",
                "level": "INFO" if inhibited else "SKIP",
                "message": detail,
            }
        )

        try:
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
                        order_direction=order_direction,
                        guild_id=guild_id,
                        mode=mode,
                    )
                    if not job:
                        has_retryable_work = self.store.has_retryable_work(
                            guild_id=guild_id,
                            mode=mode,
                            max_attempts=self.max_retries,
                            retry_failed_only=retry_failed_only,
                        )
                        if (
                            not has_retryable_work
                            and queue_refill is not None
                            and not control.stop_event.is_set()
                        ):
                            if not initial_batch_preparing_logged:
                                initial_batch_preparing_logged = True
                                event_sink(
                                    {
                                        "type": "log",
                                        "level": "INFO",
                                        "message": "Preparing first batch...",
                                    }
                                )
                            try:
                                refilled = await queue_refill()
                            except Exception:  # pragma: no cover - defensive guard
                                LOGGER.exception("Queue refill callback failed")
                                refilled = False
                            if refilled:
                                continue
                            has_retryable_work = self.store.has_retryable_work(
                                guild_id=guild_id,
                                mode=mode,
                                max_attempts=self.max_retries,
                                retry_failed_only=retry_failed_only,
                            )

                        progress = self.store.get_progress(
                            guild_id=guild_id,
                            mode=mode,
                            max_attempts=self.max_retries,
                        )
                        event_sink({"type": "progress", **progress, "elapsed_seconds": int(perf_counter() - start)})
                        if not has_retryable_work:
                            completed = True
                            elapsed_seconds = int(perf_counter() - start)
                            event_sink(
                                {
                                    "type": "completed",
                                    "elapsed_seconds": elapsed_seconds,
                                    **progress,
                                }
                            )
                            event_sink(
                                {
                                    "type": "log",
                                    "level": "OK",
                                    "message": (
                                        f"Completed: processed={progress.get('done', 0)} "
                                        f"failed={progress.get('failed', 0)} "
                                        f"remaining={progress.get('remaining', 0)} "
                                        f"elapsed={elapsed_seconds}s"
                                    ),
                                }
                            )
                            event_sink({"type": "status", "status": "completed"})
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
                        event_sink(
                            {
                                "type": "log",
                                "level": "OK",
                                "message": f"Updated message {job.discord_message_id}",
                            }
                        )
                    except DiscordApiError as exc:
                        status_label = str(exc.status_code) if exc.status_code is not None else "unknown"
                        if exc.retryable:
                            attempts = job.attempts + 1
                            retry_delay = min(30 * attempts, 300)
                            if exc.retry_after_seconds is not None:
                                retry_delay = max(retry_delay, int(exc.retry_after_seconds) + 1)
                        else:
                            attempts = self.max_retries
                            retry_delay = 0
                        self.store.mark_job_failed(
                            job.id,
                            attempts=attempts,
                            delay_seconds=retry_delay,
                            error_message=f"status={exc.status_code}",
                        )
                        if attempts >= self.max_retries:
                            detail = ""
                            if exc.status_code == 403:
                                detail = " (forbidden: missing permission or message is not editable)"
                            event_sink(
                                {
                                    "type": "log",
                                    "level": "FAIL",
                                    "message": (
                                        f"Message {job.discord_message_id} failed permanently "
                                        f"(status={status_label}){detail}"
                                    ),
                                }
                            )
                        else:
                            event_sink(
                                {
                                    "type": "log",
                                    "level": "FAIL",
                                    "message": (
                                        f"Message {job.discord_message_id} failed (status={status_label}); "
                                        f"retry {attempts}/{self.max_retries} in {retry_delay}s"
                                    ),
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
                        event_sink(
                            {
                                "type": "log",
                                "level": "FAIL",
                                "message": (
                                    f"Message {job.discord_message_id} failed unexpectedly: {type(exc).__name__}"
                                ),
                            }
                        )
                    finally:
                        self.store.release_job_lease(job.id)

                    progress = self.store.get_progress(
                        guild_id=guild_id,
                        mode=mode,
                        max_attempts=self.max_retries,
                    )
                    event_sink({"type": "progress", **progress, "elapsed_seconds": int(perf_counter() - start)})
                    delay = random.randint(
                        self.scheduler.edit_delay_min_seconds,
                        self.scheduler.edit_delay_max_seconds,
                    )
                    await self._sleep_with_stop(delay, control)
        finally:
            inhibitor.release()
            event_sink({"type": "log", "level": "INFO", "message": "Sleep inhibition released"})

        if not completed:
            event_sink({"type": "status", "status": "idle"})

    def _random_run_hours(self) -> float:
        return random.uniform(self.scheduler.run_hours_min, self.scheduler.run_hours_max)

    def _random_pause_hours(self) -> float:
        return random.uniform(self.scheduler.pause_hours_min, self.scheduler.pause_hours_max)

    async def _sleep_with_stop(self, seconds: int, control: WorkerControl) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        for _ in range(seconds):
            if control.stop_event.is_set():
                return
            await asyncio.sleep(1)

    async def _wait_until_resumed_or_stopped(self, control: WorkerControl) -> None:
        while control.pause_event.is_set() and not control.stop_event.is_set():
            await asyncio.sleep(0.2)
