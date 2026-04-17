"""Textual dashboard for VaultCord."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.events import Resize
from textual.reactive import reactive
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    ProgressBar,
    RadioButton,
    RadioSet,
    RichLog,
    Static,
)

from .constants import (
    MODE_ALL,
    MODE_LINKS,
    MODE_MEDIA,
    MODE_TEXT,
    ORDER_NEWEST,
    ORDER_OLDEST,
)
from .models import AppConfig, VaultSession
from .service import VaultService
from .worker import ScrubWorker, WorkerControl


class VaultCordTUI(App[None]):
    CSS_PATH = "tui.tcss"
    ENABLE_COMMAND_PALETTE = False
    COMPACT_BREAKPOINT = 142

    BINDINGS = [
        Binding("i", "focus_server_id", "Server ID"),
        Binding("s", "start_job", "Start"),
        Binding("p", "pause_job", "Pause"),
        Binding("r", "resume_job", "Resume"),
        Binding("x", "stop_job", "Stop"),
        Binding("g", "get_message", "Get Message"),
    ]

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
        self.selected_mode: str = MODE_ALL
        self.selected_order: str = ORDER_NEWEST

        self.total = 0
        self.processed = 0
        self.failed = 0
        self.remaining = 0
        self.elapsed_seconds = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="shell"):
            with Horizontal(id="top-strip"):
                with Vertical(id="brand-card", classes="card"):
                    yield Static("VaultCord", id="app-title")
                    yield Static("Encrypted Discord Message Vault", id="app-subtitle")
                with Vertical(id="status-card", classes="card"):
                    yield Static("Idle", id="status-chip", classes="is-idle")
                    yield Static("Guild: (none) | Mode: all | Order: newest", id="run-context")

            with Vertical(id="command-strip", classes="card"):
                yield Static("Command Strip", classes="card-title")
                with Horizontal(id="command-primary", classes="command-row"):
                    yield Static("Server ID", classes="field-label")
                    yield Input(placeholder="Paste server id", id="guild-id")
                    yield Button("Start", id="start", variant="success")
                    yield Button("Pause", id="pause", variant="warning")
                    yield Button("Resume", id="resume", variant="primary")
                    yield Button("Stop", id="stop", variant="error")
                with Horizontal(id="command-secondary", classes="command-row"):
                    yield Static("Mode", classes="field-label")
                    with RadioSet(id="mode-selector", classes="inline-radio"):
                        yield RadioButton("All", value=True, id="mode-all")
                        yield RadioButton("Text", id="mode-text")
                        yield RadioButton("Links", id="mode-links")
                        yield RadioButton("Media", id="mode-media")
                    yield Static("Order", classes="field-label")
                    with RadioSet(id="order-selector", classes="inline-radio"):
                        yield RadioButton("Newest", value=True, id="order-newest")
                        yield RadioButton("Oldest", id="order-oldest")
                    yield Checkbox("Dry Run", id="dry-run")
                    yield Checkbox("Retry Failed", id="retry-only")

            with Horizontal(id="workspace"):
                with Vertical(id="control-panel", classes="card"):
                    yield Static("Command Deck", classes="card-title")
                    yield Static("Vault ID", classes="field-label")
                    yield Input(placeholder="Paste vault id", id="vault-id")
                    yield Button("Get Message", id="get-vault", variant="primary")
                    yield Static("Retrieved: (none)", id="retrieval-output")
                    yield Static(
                        "Shortcuts: s=start  p=pause  r=resume  x=stop  g=get  i=server",
                        id="shortcut-help",
                    )

                with Vertical(id="telemetry-panel", classes="card"):
                    yield Static("Telemetry", classes="card-title")
                    yield Static("Total: 0", id="stat-total", classes="stat-row")
                    yield Static("Processed: 0", id="stat-processed", classes="stat-row")
                    yield Static("Remaining: 0", id="stat-remaining", classes="stat-row")
                    yield Static("Failed: 0", id="stat-failed", classes="stat-row")
                    yield Static("Rate: 0 msgs/hour", id="stat-rate", classes="stat-row")
                    yield Static("ETA: --", id="stat-eta", classes="stat-row")
                    yield ProgressBar(total=100, id="progress")
                    yield Static("Awaiting run...", id="completion-banner", classes="is-neutral")

            with Vertical(id="logs-panel", classes="card"):
                yield Static("Event Console", classes="card-title")
                yield RichLog(
                    id="logs",
                    wrap=False,
                    highlight=False,
                    markup=False,
                    auto_scroll=True,
                    max_lines=5000,
                )
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.2, self._drain_events)
        self._update_layout_mode(self.size.width)
        self._set_status_ui("idle")
        self.query_one("#guild-id", Input).focus()
        self._load_tui_preferences()
        self._update_run_context()
        self._write_log(
            "INFO",
            "Ready. Paste Server ID, choose mode/order, then press Start (s).",
        )

    def on_resize(self, event: Resize) -> None:
        self._update_layout_mode(event.size.width)

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        pressed_id = event.pressed.id if event.pressed is not None else ""
        radio_set = getattr(event, "radio_set", None)
        radio_set_id = radio_set.id if radio_set is not None else ""

        if radio_set_id == "mode-selector":
            mode_map = {
                "mode-all": MODE_ALL,
                "mode-text": MODE_TEXT,
                "mode-links": MODE_LINKS,
                "mode-media": MODE_MEDIA,
            }
            self.selected_mode = mode_map.get(pressed_id, MODE_ALL)
        elif radio_set_id == "order-selector":
            self.selected_order = ORDER_OLDEST if pressed_id == "order-oldest" else ORDER_NEWEST
        self._update_run_context()
        self._save_tui_preferences()

    def on_checkbox_changed(self, _: Checkbox.Changed) -> None:
        self._update_run_context()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "guild-id":
            self._update_run_context()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start":
            await self.action_start_job()
        elif event.button.id == "pause":
            await self.action_pause_job()
        elif event.button.id == "resume":
            await self.action_resume_job()
        elif event.button.id == "stop":
            await self.action_stop_job()
        elif event.button.id == "get-vault":
            await self.action_get_message()

    async def action_start_job(self) -> None:
        await self._handle_start()

    async def action_focus_server_id(self) -> None:
        self.query_one("#guild-id", Input).focus()
        self._write_log("INFO", "Server ID input focused")

    async def action_pause_job(self) -> None:
        self.worker_control.pause_event.set()
        self._set_status_ui("paused")
        self._write_log("INFO", "Pause requested")

    async def action_resume_job(self) -> None:
        self.worker_control.pause_event.clear()
        self._set_status_ui("running")
        self._write_log("INFO", "Resume requested")

    async def action_stop_job(self) -> None:
        self.worker_control.stop_event.set()
        self._write_log("INFO", "Stop requested")

    async def action_get_message(self) -> None:
        await self._handle_get_vault()

    async def _handle_start(self) -> None:
        if self.worker_task and not self.worker_task.done():
            self._write_log("SKIP", "Worker is already running")
            return

        guild_input = self.query_one("#guild-id", Input).value.strip()
        if not guild_input:
            self._write_log(
                "FAIL",
                "Server ID is required. Enable Discord Developer Mode and copy server ID.",
            )
            return

        self.current_guild_id = guild_input
        dry_run = self.query_one("#dry-run", Checkbox).value
        retry_failed_only = self.query_one("#retry-only", Checkbox).value
        self._save_tui_preferences()
        self._update_run_context()
        self._set_completion_banner("Run in progress...", state="neutral")

        self.worker_control = WorkerControl()

        if dry_run:
            self._write_log(
                "INFO",
                (
                    f"Dry run started for guild={guild_input} "
                    f"mode={self.selected_mode} order={self.selected_order}"
                ),
            )
            counts = await self.service.preview_counts(self.session, guild_input)
            self._write_log("OK", f"Dry run counts: {counts}")
            self._set_status_ui("idle")
            self._set_completion_banner("Dry run completed. No Discord edits were made.", state="success")
            return

        if retry_failed_only:
            self._write_log(
                "INFO",
                (
                    f"Retry-failed run started for guild={guild_input} "
                    f"mode={self.selected_mode} order={self.selected_order}"
                ),
            )
            reset_count = self.service.retry_failed(guild_id=guild_input, mode=self.selected_mode)
            self._write_log("OK", f"Reset {reset_count} failed jobs back to pending")
        else:
            if self.service.has_retryable_queue(guild_id=guild_input, mode=self.selected_mode):
                self._write_log(
                    "INFO",
                    (
                        f"Resume queue-first for guild={guild_input} "
                        f"mode={self.selected_mode} order={self.selected_order}"
                    ),
                )
            else:
                self._write_log(
                    "INFO",
                    (
                        f"Scrub run started for guild={guild_input} "
                        f"mode={self.selected_mode} order={self.selected_order}"
                    ),
                )
                prepare_result = await self.service.prepare_jobs(
                    self.session,
                    guild_id=guild_input,
                    mode=self.selected_mode,
                    order_direction=self.selected_order,
                    event_sink=self._emit_immediate,
                )
                self._write_log(
                    "OK",
                    (
                        "Prepared queue "
                        f"queued={prepare_result.queued} skipped={prepare_result.skipped} "
                        f"already_ref={prepare_result.already_referenced}"
                    ),
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
                mode=self.selected_mode,
                retry_failed_only=retry_failed_only,
                order_direction=self.selected_order,
                control=self.worker_control,
                event_sink=self._emit_immediate,
            )
        )
        self._set_status_ui("running")

    async def _handle_get_vault(self) -> None:
        vault_id = self.query_one("#vault-id", Input).value.strip()
        if not vault_id:
            self._write_log("FAIL", "Vault ID is required for retrieval")
            return

        try:
            payload = self.service.decrypt_vault_message(vault_id=vault_id, password=self.session.password)
        except Exception:
            self._write_log("FAIL", "Unable to decrypt vault message")
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
                self._set_status_ui(str(event["status"]))
                if event.get("status") in {"idle", "completed"}:
                    self.worker_task = None
            elif event["type"] == "progress":
                self._update_progress(event)
            elif event["type"] == "completed":
                self._handle_completed(event)
            elif event["type"] == "log":
                level = str(event.get("level", "INFO"))
                message = str(event.get("message", ""))
                self._write_log(level, message)

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
        if self.status.lower() in {"running", "paused"} and self.remaining > 0:
            self._set_completion_banner("Run in progress...", state="neutral")

    def _handle_completed(self, payload: dict[str, Any]) -> None:
        done = int(payload.get("done", 0))
        failed = int(payload.get("failed", 0))
        remaining = int(payload.get("remaining", 0))
        elapsed_seconds = int(payload.get("elapsed_seconds", 0))
        message = (
            f"Run completed: processed={done} failed={failed} "
            f"remaining={remaining} elapsed={elapsed_seconds}s"
        )
        self._write_log("OK", message)
        if failed > 0:
            self._set_completion_banner(
                "Completed with failures. Use Retry Failed to continue remaining work.",
                state="fail",
            )
            self._write_log("INFO", "Use Retry Failed to requeue failed items.")
        elif remaining == 0:
            self._set_completion_banner(
                "Completed: all queued messages archived and replaced.",
                state="success",
            )
        else:
            self._set_completion_banner(
                "Run ended before queue was fully processed. Resume to continue.",
                state="fail",
            )
        self._set_status_ui("completed")

    def _load_tui_preferences(self) -> None:
        try:
            payload = self.service.store.read_setting("tui_preferences") or {}
            mode = str(payload.get("mode", MODE_ALL))
            order_direction = str(payload.get("order_direction", ORDER_NEWEST))
            mode_map = {
                MODE_ALL: "mode-all",
                MODE_TEXT: "mode-text",
                MODE_LINKS: "mode-links",
                MODE_MEDIA: "mode-media",
            }
            mode_radio_id = mode_map.get(mode, "mode-all")
            self.query_one(f"#{mode_radio_id}", RadioButton).value = True
            self.selected_mode = mode if mode in mode_map else MODE_ALL
            if order_direction == ORDER_OLDEST:
                self.selected_order = ORDER_OLDEST
                self.query_one("#order-oldest", RadioButton).value = True
        except Exception:
            self.selected_mode = MODE_ALL
            self.selected_order = ORDER_NEWEST

    def _save_tui_preferences(self) -> None:
        try:
            self.service.store.save_setting(
                "tui_preferences",
                {
                    "mode": self.selected_mode,
                    "order_direction": self.selected_order,
                },
            )
        except Exception:
            return

    def _set_status_ui(self, status: str) -> None:
        normalized = status.lower().strip()
        if normalized not in {"idle", "running", "paused", "completed"}:
            normalized = "idle"

        self.status = normalized.capitalize()
        chip = self.query_one("#status-chip", Static)
        chip.update(self.status)
        chip.set_class(normalized == "idle", "is-idle")
        chip.set_class(normalized == "running", "is-running")
        chip.set_class(normalized == "paused", "is-paused")
        chip.set_class(normalized == "completed", "is-completed")
        self._sync_action_buttons(normalized)

    def _sync_action_buttons(self, status: str) -> None:
        start = self.query_one("#start", Button)
        pause = self.query_one("#pause", Button)
        resume = self.query_one("#resume", Button)
        stop = self.query_one("#stop", Button)

        start.disabled = status in {"running", "paused"}
        pause.disabled = status != "running"
        resume.disabled = status != "paused"
        stop.disabled = status not in {"running", "paused"}

    def _set_completion_banner(self, message: str, *, state: str) -> None:
        banner = self.query_one("#completion-banner", Static)
        banner.update(message)
        banner.set_class(state == "success", "is-success")
        banner.set_class(state == "fail", "is-fail")
        banner.set_class(state == "neutral", "is-neutral")

    def _update_run_context(self) -> None:
        try:
            guild_value = self.query_one("#guild-id", Input).value.strip()
            dry_run = self.query_one("#dry-run", Checkbox).value
            retry_failed = self.query_one("#retry-only", Checkbox).value
        except NoMatches:
            return
        context = f"Guild: {guild_value or '(none)'} | Mode: {self.selected_mode} | Order: {self.selected_order}"
        if dry_run:
            context += " | Dry Run"
        if retry_failed:
            context += " | Retry Failed"
        self.query_one("#run-context", Static).update(context)

    def _update_layout_mode(self, width: int) -> None:
        self.set_class(width < self.COMPACT_BREAKPOINT, "-compact")

    def _write_log(self, level: str, message: str) -> None:
        log_widget = self.query_one("#logs", RichLog)
        width = log_widget.size.width or 120
        log_widget.write(self._render_log(level, message, max_width=width))

    @staticmethod
    def _truncate_for_log(message: str, max_chars: int) -> str:
        if max_chars < 1:
            return ""
        single_line = " ".join(str(message).replace("\n", " ").replace("\r", " ").split())
        if len(single_line) <= max_chars:
            return single_line
        if max_chars == 1:
            return "…"
        return f"{single_line[: max_chars - 1]}…"

    def _render_log(self, level: str, message: str, *, max_width: int = 120) -> Text:
        ts = datetime.now().strftime("%H:%M:%S")
        norm_level = level.upper().strip()
        style = {
            "OK": "green",
            "FAIL": "red",
            "SKIP": "yellow",
            "INFO": "cyan",
        }.get(norm_level, "white")
        max_message_chars = max(20, max_width - 22)
        safe_message = self._truncate_for_log(message, max_message_chars)

        row = Text()
        row.append(ts, style="dim")
        row.append(" | ", style="dim")
        row.append(f"{norm_level:<5}", style=style)
        row.append(" | ", style="dim")
        row.append(safe_message)
        return row
