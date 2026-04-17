"""Textual dashboard for VaultCord."""

from __future__ import annotations

import asyncio
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Button, Checkbox, Footer, Header, Input, ProgressBar, RadioButton, RadioSet, RichLog, Static

from .constants import MODE_ALL, MODE_LINKS, MODE_MEDIA, MODE_TEXT
from .models import AppConfig, VaultSession
from .service import VaultService
from .worker import ScrubWorker, WorkerControl


class VaultCordTUI(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #main {
        layout: horizontal;
        height: 1fr;
    }

    #left-panel {
        width: 33;
        border: round #666666;
        padding: 1;
    }

    #right-panel {
        width: 1fr;
        border: round #666666;
        padding: 1;
    }

    #logs {
        height: 12;
        border: round #666666;
        margin-top: 1;
    }

    .stat {
        height: 1;
    }

    .action {
        margin-top: 1;
    }
    """

    status: reactive[str] = reactive("Idle")

    def __init__(self, *, service: VaultService, session: VaultSession, config: AppConfig) -> None:
        super().__init__()
        self.service = service
        self.session = session
        self.config = config

        self.event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.worker_control = WorkerControl()
        self.worker_task: asyncio.Task[None] | None = None
        self.current_guild_id: str | None = None
        self.current_mode: str = MODE_ALL

        self.total = 0
        self.processed = 0
        self.failed = 0
        self.remaining = 0
        self.elapsed_seconds = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("VaultCord | Status: Idle", id="status-line")

        with RadioSet(id="mode-selector"):
            yield RadioButton("All", value=True, id="mode-all")
            yield RadioButton("Text", id="mode-text")
            yield RadioButton("Links", id="mode-links")
            yield RadioButton("Media", id="mode-media")

        with Horizontal(id="main"):
            with Vertical(id="left-panel"):
                yield Static("Controls")
                yield Input(placeholder="Guild ID", id="guild-id")
                yield Input(placeholder="Vault ID (for retrieval)", id="vault-id")
                yield Button("Start", id="start", variant="success", classes="action")
                yield Button("Pause", id="pause", variant="warning", classes="action")
                yield Button("Resume", id="resume", variant="primary", classes="action")
                yield Button("Stop", id="stop", variant="error", classes="action")
                yield Button("Get Message", id="get-vault", variant="default", classes="action")
                yield Checkbox("Dry Run", id="dry-run")
                yield Checkbox("Retry Failed Only", id="retry-only")

            with Vertical(id="right-panel"):
                yield Static("Progress & Stats")
                yield Static("Total: 0", id="stat-total", classes="stat")
                yield Static("Processed: 0", id="stat-processed", classes="stat")
                yield Static("Remaining: 0", id="stat-remaining", classes="stat")
                yield Static("Failed: 0", id="stat-failed", classes="stat")
                yield Static("Rate: 0 msgs/hour", id="stat-rate", classes="stat")
                yield Static("ETA: --", id="stat-eta", classes="stat")
                yield ProgressBar(total=100, id="progress")
                yield Static("Retrieved: (none)", id="retrieval-output")

        yield RichLog(id="logs", wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.2, self._drain_events)

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        mode_map = {
            "mode-all": MODE_ALL,
            "mode-text": MODE_TEXT,
            "mode-links": MODE_LINKS,
            "mode-media": MODE_MEDIA,
        }
        self.current_mode = mode_map.get(event.pressed.id or "", MODE_ALL)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start":
            await self._handle_start()
        elif event.button.id == "pause":
            self.worker_control.pause_event.set()
        elif event.button.id == "resume":
            self.worker_control.pause_event.clear()
        elif event.button.id == "stop":
            self.worker_control.stop_event.set()
        elif event.button.id == "get-vault":
            await self._handle_get_vault()

    async def _handle_start(self) -> None:
        if self.worker_task and not self.worker_task.done():
            await self.event_queue.put({"type": "log", "level": "SKIP", "message": "Worker is already running"})
            return

        guild_input = self.query_one("#guild-id", Input).value.strip()
        if not guild_input:
            await self.event_queue.put({"type": "log", "level": "FAIL", "message": "Guild ID is required"})
            return

        self.current_guild_id = guild_input
        dry_run = self.query_one("#dry-run", Checkbox).value
        retry_failed_only = self.query_one("#retry-only", Checkbox).value

        self.worker_control = WorkerControl()

        if dry_run:
            counts = await self.service.preview_counts(self.session, guild_input)
            await self.event_queue.put({"type": "log", "level": "OK", "message": f"Dry run counts: {counts}"})
            return

        if retry_failed_only:
            reset_count = self.service.retry_failed(guild_id=guild_input, mode=self.current_mode)
            await self.event_queue.put(
                {
                    "type": "log",
                    "level": "OK",
                    "message": f"Reset {reset_count} failed jobs back to pending",
                }
            )
        else:
            prepare_result = await self.service.prepare_jobs(
                self.session,
                guild_id=guild_input,
                mode=self.current_mode,
                event_sink=self._emit_immediate,
            )
            await self.event_queue.put(
                {
                    "type": "log",
                    "level": "OK",
                    "message": (
                        "Prepared queue "
                        f"queued={prepare_result.queued} skipped={prepare_result.skipped} "
                        f"already_ref={prepare_result.already_referenced}"
                    ),
                }
            )

        worker = ScrubWorker(
            store=self.service.store,
            session=self.session,
            scheduler=self.config.scheduler,
            request_timeout_seconds=self.config.request_timeout_seconds,
            max_retries=self.config.max_retries,
        )

        self.worker_task = asyncio.create_task(
            worker.run(
                guild_id=guild_input,
                mode=self.current_mode,
                retry_failed_only=retry_failed_only,
                control=self.worker_control,
                event_sink=self._emit_immediate,
            )
        )

    async def _handle_get_vault(self) -> None:
        vault_id = self.query_one("#vault-id", Input).value.strip()
        if not vault_id:
            await self.event_queue.put(
                {"type": "log", "level": "FAIL", "message": "Vault ID is required for retrieval"}
            )
            return

        try:
            payload = self.service.decrypt_vault_message(vault_id=vault_id, password=self.session.password)
        except Exception:
            await self.event_queue.put(
                {"type": "log", "level": "FAIL", "message": "Unable to decrypt vault message"}
            )
            self.query_one("#retrieval-output", Static).update("Retrieved: decrypt failed")
            return

        content = str(payload.get("content", ""))
        preview = content if len(content) <= 220 else f"{content[:220]}..."
        self.query_one("#retrieval-output", Static).update(f"Retrieved: {preview}")

    def _emit_immediate(self, event: dict[str, Any]) -> None:
        self.event_queue.put_nowait(event)

    async def _drain_events(self) -> None:
        while not self.event_queue.empty():
            event = await self.event_queue.get()
            if event["type"] == "status":
                self.status = str(event["status"]).capitalize()
                self.query_one("#status-line", Static).update(f"VaultCord | Status: {self.status}")
            elif event["type"] == "progress":
                self._update_progress(event)
            elif event["type"] == "log":
                log_widget = self.query_one("#logs", RichLog)
                level = str(event.get("level", "INFO"))
                message = str(event.get("message", ""))
                log_widget.write(f"[{level}] {message}")

    def _update_progress(self, payload: dict[str, Any]) -> None:
        self.total = int(payload.get("total", 0))
        self.processed = int(payload.get("done", 0))
        self.failed = int(payload.get("failed", 0))
        self.remaining = int(payload.get("remaining", 0))
        self.elapsed_seconds = int(payload.get("elapsed_seconds", 0))

        percent = 0.0
        if self.total > 0:
            percent = (self.processed + self.failed) / self.total

        progress_widget = self.query_one("#progress", ProgressBar)
        progress_widget.update(total=100, progress=max(0, min(int(percent * 100), 100)))

        self.query_one("#stat-total", Static).update(f"Total: {self.total}")
        self.query_one("#stat-processed", Static).update(f"Processed: {self.processed}")
        self.query_one("#stat-remaining", Static).update(f"Remaining: {self.remaining}")
        self.query_one("#stat-failed", Static).update(f"Failed: {self.failed}")

        rate = 0.0
        if self.elapsed_seconds > 0:
            rate = (self.processed / self.elapsed_seconds) * 3600
        self.query_one("#stat-rate", Static).update(f"Rate: {rate:.2f} msgs/hour")

        eta = "--"
        if rate > 0 and self.remaining > 0:
            eta_hours = self.remaining / rate
            eta = f"{eta_hours:.2f}h"
        self.query_one("#stat-eta", Static).update(f"ETA: {eta}")
