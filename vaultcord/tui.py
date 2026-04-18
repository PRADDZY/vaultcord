"""Prompt-toolkit dashboard for VaultCord."""

from __future__ import annotations

import asyncio
import threading
from collections import Counter, deque
from datetime import datetime
from queue import Empty, Queue
from time import monotonic
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.input import DummyInput
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, VSplit
from prompt_toolkit.layout.scrollable_pane import ScrollablePane
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.shortcuts import set_title
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Button, Checkbox, Frame, Label, RadioList, TextArea

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


class VaultCordTUI:
    """Interactive prompt_toolkit dashboard with worker/event integration."""

    LOG_MAX_LINES = 5000
    ESC_CONFIRM_SECONDS = 5.0

    def __init__(self, *, service: VaultService, session: VaultSession, config: AppConfig) -> None:
        self.service = service
        self.session = session
        self.config = config

        self.status = "Idle"
        self.current_guild_id: str | None = None
        self.total = 0
        self.processed = 0
        self.failed = 0
        self.remaining = 0
        self.elapsed_seconds = 0
        self.rate_per_hour = 0.0
        self.eta_text = "--"
        self.completion_message = "Awaiting run..."
        self.completion_level = "INFO"

        self.event_queue: Queue[dict[str, Any]] = Queue()
        self.logs: deque[str] = deque(maxlen=self.LOG_MAX_LINES)
        self._pending_exit_deadline = 0.0

        self._start_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._worker_loop: asyncio.AbstractEventLoop | None = None
        self._worker_control: WorkerControl | None = None
        self._thread_lock = threading.Lock()

        self.mode_list = RadioList(
            values=[
                (MODE_ALL, "All"),
                (MODE_TEXT, "Text"),
                (MODE_LINKS, "Links"),
                (MODE_MEDIA, "Media"),
            ]
        )
        self.order_list = RadioList(
            values=[
                (ORDER_NEWEST, "Newest"),
                (ORDER_OLDEST, "Oldest"),
            ]
        )
        self.dry_run_checkbox = Checkbox(text="Dry Run")
        self.retry_only_checkbox = Checkbox(text="Retry Failed")

        self.guild_input = TextArea(multiline=False, height=1, prompt="Server ID: ", scrollbar=False)
        self.vault_id_input = TextArea(multiline=False, height=1, prompt="Vault ID: ", scrollbar=False)

        self.start_button = Button(text="Start", handler=self._on_start_pressed)
        self.pause_button = Button(text="Pause", handler=self._on_pause_pressed)
        self.resume_button = Button(text="Resume", handler=self._on_resume_pressed)
        self.stop_button = Button(text="Stop", handler=self._on_stop_pressed)
        self.get_button = Button(text="Get Message", handler=self._on_get_pressed)

        self.status_label = Label(text="")
        self.context_label = Label(text="")
        self.stats_label = Label(text="")
        self.progress_label = Label(text="")
        self.completion_label = Label(text="")
        self.retrieval_label = Label(text="Retrieved: (none)")
        self.help_label = Label(text="Shortcuts: s p r x g i v Esc | Paste: Ctrl+V")
        self.log_area = TextArea(
            text="",
            read_only=True,
            focusable=False,
            scrollbar=True,
            multiline=True,
            wrap_lines=False,
        )

        self._button_enabled: dict[str, bool] = {
            "start": True,
            "pause": False,
            "resume": False,
            "stop": False,
            "get": True,
        }

        self._load_tui_preferences()
        self._update_context_label()
        self._refresh_status_widgets()

        self.application = self._build_application()

    def run(self) -> None:
        set_title("VaultCord")
        self._append_log("INFO", "Ready. Paste Server ID, choose mode/order, then press Start (s).")
        try:
            self.application.run()
        except Exception as exc:
            message = str(exc).lower()
            if "window too small" not in message:
                raise
            self._run_small_window_fallback()

    def _run_small_window_fallback(self) -> None:
        kb = KeyBindings()

        @kb.add("q")
        @kb.add("escape")
        @kb.add("c-c")
        def _exit(event: Any) -> None:
            event.app.exit()

        fallback = Application(
            layout=Layout(
                Frame(
                    HSplit(
                        [
                            Label(text="Terminal window is too small for the full VaultCord dashboard."),
                            Label(text="Resize the terminal and relaunch `vault tui`."),
                            Label(text="Press Esc or q to exit."),
                        ],
                        padding=0,
                    ),
                    title="VaultCord",
                    style="class:panel",
                )
            ),
            key_bindings=kb,
            style=self._build_style(),
            full_screen=True,
            mouse_support=True,
        )
        fallback.run()

    def _build_application(self) -> Application[Any]:
        kwargs: dict[str, Any] = {
            "layout": Layout(self._build_root_container(), focused_element=self.guild_input),
            "key_bindings": self._build_keybindings(),
            "style": self._build_style(),
            "full_screen": True,
            "mouse_support": True,
            "refresh_interval": 0.2,
            "before_render": self._before_render,
        }
        try:
            return Application(**kwargs)
        except Exception:
            return Application(**kwargs, input=DummyInput(), output=DummyOutput())

    def _build_root_container(self) -> HSplit:
        top_bar = HSplit(
            [
                self.status_label,
                self.context_label,
                self.help_label,
            ],
            padding=0,
        )

        control_body = HSplit(
            [
                self.guild_input,
                VSplit([self.start_button, self.pause_button], padding=1),
                VSplit([self.resume_button, self.stop_button], padding=1),
                Label(text="Mode"),
                self.mode_list,
                Label(text="Order"),
                self.order_list,
                self.dry_run_checkbox,
                self.retry_only_checkbox,
                self.vault_id_input,
                self.get_button,
                self.retrieval_label,
            ],
            padding=0,
        )

        telemetry_body = HSplit(
            [
                self.stats_label,
                self.progress_label,
                self.completion_label,
            ],
            padding=0,
        )

        workspace = HSplit(
            [
                Frame(ScrollablePane(control_body, show_scrollbar=True), title="Command Deck", style="class:panel"),
                Frame(telemetry_body, title="Telemetry", style="class:panel"),
            ],
            padding=0,
        )

        logs = Frame(self.log_area, title="Event Console", style="class:panel")

        return HSplit(
            [
                Frame(top_bar, title="VaultCord", style="class:panel"),
                workspace,
                logs,
            ],
            padding=0,
        )

    def _build_style(self) -> Style:
        return Style.from_dict(
            {
                "": "#d2d8e0 bg:#081018",
                "frame.border": "#31506b",
                "frame.label": "bold #9fd0ff",
                "panel": "bg:#0e1a26",
                "button": "bg:#19324a #cfe6ff",
                "button.arrow": "#9fd0ff",
                "checkbox": "#cfe6ff",
                "radio": "#cfe6ff",
                "text-area": "bg:#0b141f #d9e2ee",
                "scrollbar.background": "bg:#0b141f",
                "scrollbar.button": "bg:#35536e",
            }
        )

    def _build_keybindings(self) -> KeyBindings:
        kb = KeyBindings()
        not_input_focus = Condition(lambda: not self._text_input_focused())
        input_focus = Condition(self._text_input_focused)

        @kb.add("tab")
        def _focus_next(event: Any) -> None:
            event.app.layout.focus_next()

        @kb.add("s-tab")
        def _focus_previous(event: Any) -> None:
            event.app.layout.focus_previous()

        @kb.add("s", filter=not_input_focus)
        def _start(_: Any) -> None:
            self._on_start_pressed()

        @kb.add("p", filter=not_input_focus)
        def _pause(_: Any) -> None:
            self._on_pause_pressed()

        @kb.add("r", filter=not_input_focus)
        def _resume(_: Any) -> None:
            self._on_resume_pressed()

        @kb.add("x", filter=not_input_focus)
        def _stop(_: Any) -> None:
            self._on_stop_pressed()

        @kb.add("g", filter=not_input_focus)
        def _get(_: Any) -> None:
            self._on_get_pressed()

        @kb.add("i", filter=not_input_focus)
        def _focus_server(event: Any) -> None:
            event.app.layout.focus(self.guild_input)
            self._append_log("INFO", "Server ID input focused")

        @kb.add("v", filter=not_input_focus)
        def _focus_vault_id(event: Any) -> None:
            event.app.layout.focus(self.vault_id_input)
            self._append_log("INFO", "Vault ID input focused")

        @kb.add("c-v", filter=input_focus)
        @kb.add("s-insert", filter=input_focus)
        def _paste_clipboard(event: Any) -> None:
            try:
                data = event.app.clipboard.get_data()
                text = str(data.text) if data and data.text is not None else ""
            except Exception:
                text = ""
            self._insert_into_focused_input(text)

        @kb.add("<bracketed-paste>", filter=input_focus)
        def _paste_bracketed(event: Any) -> None:
            self._insert_into_focused_input(str(getattr(event, "data", "") or ""))

        @kb.add("escape")
        def _escape(_: Any) -> None:
            self._on_escape()

        @kb.add("c-c")
        def _ctrl_c(_: Any) -> None:
            self._on_escape()

        return kb

    def _before_render(self, _: Application[Any]) -> None:
        self._drain_events()
        self._update_context_label()
        self._refresh_status_widgets()

    def _text_input_focused(self) -> bool:
        try:
            layout = self.application.layout
            return layout.has_focus(self.guild_input) or layout.has_focus(self.vault_id_input)
        except Exception:
            return False

    def _insert_into_focused_input(self, text: str) -> None:
        if not text:
            return
        try:
            layout = self.application.layout
            if layout.has_focus(self.guild_input):
                self.guild_input.text = f"{self.guild_input.text}{text}"
                return
            if layout.has_focus(self.vault_id_input):
                self.vault_id_input.text = f"{self.vault_id_input.text}{text}"
        except Exception:
            return

    def _drain_events(self) -> None:
        while True:
            try:
                event = self.event_queue.get_nowait()
            except Empty:
                break
            event_type = event.get("type")
            if event_type == "status":
                self._set_status(str(event.get("status", "idle")))
            elif event_type == "progress":
                self._update_progress(event)
            elif event_type == "completed":
                self._handle_completed(event)
            elif event_type == "log":
                self._append_log(
                    str(event.get("level", "INFO")),
                    str(event.get("message", "")),
                )
            elif event_type == "banner":
                self.completion_level = str(event.get("level", "INFO")).upper()
                self.completion_message = str(event.get("message", ""))

    def _emit_event(self, event: dict[str, Any]) -> None:
        self.event_queue.put(event)
        if self.application.is_running:
            self.application.invalidate()

    def _on_start_pressed(self) -> None:
        if not self._button_enabled["start"]:
            return
        guild_id = self.guild_input.text.strip()
        if not guild_id:
            self._append_log("FAIL", "Server ID is required. Enable Developer Mode and copy server ID.")
            return
        if self._is_starting():
            self._append_log("SKIP", "A start operation is already in progress.")
            return
        if self._is_worker_active():
            self._append_log("SKIP", "Worker is already running.")
            return

        mode = str(self.mode_list.current_value or MODE_ALL)
        order = str(self.order_list.current_value or ORDER_NEWEST)
        dry_run = bool(self.dry_run_checkbox.checked)
        retry_failed_only = bool(self.retry_only_checkbox.checked)

        self.current_guild_id = guild_id
        self.completion_level = "INFO"
        self.completion_message = "Run in progress..."
        self._save_tui_preferences()

        self._set_status("running")
        self._start_thread = threading.Thread(
            target=self._start_flow_thread,
            args=(guild_id, mode, order, dry_run, retry_failed_only),
            daemon=True,
        )
        self._start_thread.start()

    def _start_flow_thread(
        self,
        guild_id: str,
        mode: str,
        order: str,
        dry_run: bool,
        retry_failed_only: bool,
    ) -> None:
        try:
            if dry_run:
                self._emit_event(
                    {
                        "type": "log",
                        "level": "INFO",
                        "message": f"Dry run started for guild={guild_id} mode={mode} order={order}",
                    }
                )
                counts = asyncio.run(self.service.preview_counts(self.session, guild_id))
                self._emit_event({"type": "log", "level": "OK", "message": f"Dry run counts: {counts}"})
                self._emit_event(
                    {
                        "type": "banner",
                        "level": "OK",
                        "message": "Dry run completed. No Discord edits were made.",
                    }
                )
                self._emit_event({"type": "status", "status": "idle"})
                return

            if retry_failed_only:
                self._emit_event(
                    {
                        "type": "log",
                        "level": "INFO",
                        "message": f"Retry-failed run started for guild={guild_id} mode={mode} order={order}",
                    }
                )
                reset_count = self.service.retry_failed(guild_id=guild_id, mode=mode)
                self._emit_event(
                    {
                        "type": "log",
                        "level": "OK",
                        "message": f"Reset {reset_count} failed jobs back to pending",
                    }
                )
            else:
                if self.service.has_retryable_queue(guild_id=guild_id, mode=mode):
                    self._emit_event(
                        {
                            "type": "log",
                            "level": "INFO",
                            "message": f"Resume queue-first for guild={guild_id} mode={mode} order={order}",
                        }
                    )
                else:
                    self._emit_event(
                        {
                            "type": "log",
                            "level": "INFO",
                            "message": (
                                f"Incremental batching enabled for guild={guild_id} mode={mode} "
                                f"order={order} batch_size={self.config.batch_prepare_size}"
                            ),
                        }
                    )

            self._start_worker_thread(
                guild_id=guild_id,
                mode=mode,
                retry_failed_only=retry_failed_only,
                order_direction=order,
            )
        except Exception:
            self._emit_event(
                {
                    "type": "log",
                    "level": "FAIL",
                    "message": "Start flow failed unexpectedly. Check logs and retry.",
                }
            )
            self._emit_event(
                {
                    "type": "banner",
                    "level": "FAIL",
                    "message": "Run setup failed. Check logs and retry.",
                }
            )
            self._emit_event({"type": "status", "status": "idle"})

    def _start_worker_thread(
        self,
        *,
        guild_id: str,
        mode: str,
        retry_failed_only: bool,
        order_direction: str,
    ) -> None:
        with self._thread_lock:
            if self._worker_thread and self._worker_thread.is_alive():
                self._emit_event({"type": "log", "level": "SKIP", "message": "Worker is already running."})
                return
            self._worker_thread = threading.Thread(
                target=self._worker_thread_main,
                kwargs={
                    "guild_id": guild_id,
                    "mode": mode,
                    "retry_failed_only": retry_failed_only,
                    "order_direction": order_direction,
                },
                daemon=True,
            )
            self._worker_thread.start()

    def _worker_thread_main(
        self,
        *,
        guild_id: str,
        mode: str,
        retry_failed_only: bool,
        order_direction: str,
    ) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._worker_loop = loop
        self._worker_control = WorkerControl()

        worker = ScrubWorker(
            store=self.service.store,
            session=self.session,
            scheduler=self.config.scheduler,
            request_timeout_seconds=self.config.request_timeout_seconds,
            max_retries=self.config.max_retries,
        )
        had_existing_queue = retry_failed_only or self.service.has_retryable_queue(guild_id=guild_id, mode=mode)

        async def _run_worker() -> None:
            scan_exhausted = False
            total_queued = 0
            total_failed_channels = 0
            aggregate_errors: Counter[str] = Counter()

            async def _queue_refill() -> bool:
                nonlocal scan_exhausted, total_queued, total_failed_channels, aggregate_errors
                if retry_failed_only:
                    return False
                if scan_exhausted:
                    return False

                prepare_result = await self.service.prepare_jobs_batch(
                    self.session,
                    guild_id=guild_id,
                    mode=mode,
                    order_direction=order_direction,
                    batch_size=self.config.batch_prepare_size,
                    event_sink=self._emit_event,
                )
                total_queued += prepare_result.queued
                total_failed_channels += prepare_result.failed_channels
                aggregate_errors.update(prepare_result.fetch_error_breakdown)

                if (
                    prepare_result.queued > 0
                    or prepare_result.skipped > 0
                    or prepare_result.already_referenced > 0
                    or prepare_result.failed_channels > 0
                ):
                    failed_suffix = ""
                    if prepare_result.failed_channels > 0:
                        breakdown = self._format_fetch_error_breakdown(prepare_result.fetch_error_breakdown)
                        failed_suffix = f" failed_channels={prepare_result.failed_channels} ({breakdown})"
                    self._emit_event(
                        {
                            "type": "log",
                            "level": "OK",
                            "message": (
                                "Batch prepared "
                                f"queued={prepare_result.queued} skipped={prepare_result.skipped} "
                                f"already_ref={prepare_result.already_referenced}{failed_suffix}"
                            ),
                        }
                    )
                if prepare_result.exhausted:
                    scan_exhausted = True
                    self._emit_event(
                        {
                            "type": "log",
                            "level": "INFO",
                            "message": "Scan exhausted: no further messages to queue",
                        }
                    )
                    if not had_existing_queue and total_queued == 0:
                        if total_failed_channels > 0:
                            breakdown = self._format_fetch_error_breakdown(dict(aggregate_errors))
                            self._emit_event(
                                {
                                    "type": "banner",
                                    "level": "SKIP",
                                    "message": (
                                        "No queueable messages found; some channels were inaccessible "
                                        f"({breakdown})."
                                    ),
                                }
                            )
                        else:
                            self._emit_event(
                                {
                                    "type": "banner",
                                    "level": "INFO",
                                    "message": "No eligible user messages found for selected mode/order.",
                                }
                            )
                return prepare_result.queued > 0

            await worker.run(
                guild_id=guild_id,
                mode=mode,
                retry_failed_only=retry_failed_only,
                order_direction=order_direction,
                control=self._worker_control or WorkerControl(),
                event_sink=self._emit_event,
                queue_refill=_queue_refill,
            )

        try:
            loop.run_until_complete(_run_worker())
        finally:
            self._worker_control = None
            self._worker_loop = None
            loop.close()

    def _on_pause_pressed(self) -> None:
        if not self._button_enabled["pause"]:
            return
        if not self._run_worker_control_call(lambda c: c.pause_event.set()):
            self._append_log("SKIP", "No active worker to pause.")
            return
        self._set_status("paused")
        self._append_log("INFO", "Pause requested")

    def _on_resume_pressed(self) -> None:
        if not self._button_enabled["resume"]:
            return
        if not self._run_worker_control_call(lambda c: c.pause_event.clear()):
            self._append_log("SKIP", "No paused worker to resume.")
            return
        self._set_status("running")
        self._append_log("INFO", "Resume requested")

    def _on_stop_pressed(self) -> None:
        if not self._button_enabled["stop"]:
            return
        if not self._run_worker_control_call(lambda c: c.stop_event.set()):
            self._append_log("SKIP", "No active worker to stop.")
            return
        self._append_log("INFO", "Stop requested")

    def _run_worker_control_call(self, action: Any) -> bool:
        control = self._worker_control
        loop = self._worker_loop
        if not control or not loop:
            return False
        loop.call_soon_threadsafe(action, control)
        return True

    def _on_get_pressed(self) -> None:
        if not self._button_enabled["get"]:
            return
        vault_id = self.vault_id_input.text.strip()
        if not vault_id:
            self._append_log("FAIL", "Vault ID is required for retrieval")
            return
        try:
            payload = self.service.decrypt_vault_message(vault_id=vault_id, password=self.session.password)
        except Exception:
            self._append_log("FAIL", "Unable to decrypt vault message")
            self.retrieval_label.text = "Retrieved: decrypt failed"
            return
        content = str(payload.get("content", ""))
        preview = content if len(content) <= 220 else f"{content[:217]}..."
        self.retrieval_label.text = f"Retrieved: {preview}"

    def _on_escape(self) -> None:
        if self._is_worker_active() or self._is_starting():
            now = monotonic()
            if now > self._pending_exit_deadline:
                self._pending_exit_deadline = now + self.ESC_CONFIRM_SECONDS
                self._append_log(
                    "FAIL",
                    "Worker is active. Press Esc again within 5s to stop and exit.",
                )
                return
            self._run_worker_control_call(lambda c: c.stop_event.set())
            self.application.exit()
            return
        self.application.exit()

    def _is_worker_active(self) -> bool:
        return bool(self._worker_thread and self._worker_thread.is_alive())

    def _is_starting(self) -> bool:
        return bool(self._start_thread and self._start_thread.is_alive())

    def _set_status(self, status: str) -> None:
        normalized = status.lower().strip()
        if normalized not in {"idle", "running", "paused", "completed"}:
            normalized = "idle"
        self.status = normalized.capitalize()
        self._sync_action_buttons(normalized)

    def _sync_action_buttons(self, status: str) -> None:
        self._button_enabled["start"] = status in {"idle", "completed"}
        self._button_enabled["pause"] = status == "running"
        self._button_enabled["resume"] = status == "paused"
        self._button_enabled["stop"] = status in {"running", "paused"}
        self._button_enabled["get"] = True

    def _update_progress(self, payload: dict[str, Any]) -> None:
        self.total = int(payload.get("total", 0))
        self.processed = int(payload.get("done", 0))
        self.failed = int(payload.get("failed", 0))
        self.remaining = int(payload.get("remaining", 0))
        self.elapsed_seconds = int(payload.get("elapsed_seconds", 0))
        self.rate_per_hour = (self.processed / self.elapsed_seconds) * 3600 if self.elapsed_seconds > 0 else 0.0
        if self.rate_per_hour > 0 and self.remaining > 0:
            eta_hours = self.remaining / self.rate_per_hour
            self.eta_text = f"{eta_hours:.2f}h"
        else:
            self.eta_text = "--"
        if self.status.lower() in {"running", "paused"} and self.remaining > 0:
            self.completion_level = "INFO"
            self.completion_message = "Run in progress..."

    def _handle_completed(self, payload: dict[str, Any]) -> None:
        done = int(payload.get("done", 0))
        failed = int(payload.get("failed", 0))
        remaining = int(payload.get("remaining", 0))
        elapsed_seconds = int(payload.get("elapsed_seconds", 0))
        has_preexisting_completion_hint = self.completion_message not in {"Awaiting run...", "Run in progress..."}
        self._append_log(
            "OK",
            f"Run completed: processed={done} failed={failed} remaining={remaining} elapsed={elapsed_seconds}s",
        )
        if failed > 0:
            self.completion_level = "FAIL"
            self.completion_message = "Completed with failures. Use Retry Failed to continue remaining work."
            self._append_log("INFO", "Use Retry Failed to requeue failed items.")
        elif done == 0 and remaining == 0 and has_preexisting_completion_hint:
            # Preserve explicit zero-queue completion hints emitted during scan/refill.
            pass
        elif remaining == 0:
            self.completion_level = "OK"
            self.completion_message = "Completed: all queued messages archived and replaced."
        else:
            self.completion_level = "FAIL"
            self.completion_message = "Run ended before queue was fully processed. Resume to continue."
        self._set_status("completed")

    def _update_context_label(self) -> None:
        guild = self.guild_input.text.strip()
        mode = str(self.mode_list.current_value or MODE_ALL)
        order = str(self.order_list.current_value or ORDER_NEWEST)
        context = f"Guild: {guild or '(none)'} | Mode: {mode} | Order: {order}"
        if self.dry_run_checkbox.checked:
            context += " | Dry Run"
        if self.retry_only_checkbox.checked:
            context += " | Retry Failed"
        self.context_label.text = self._truncate_for_log(context, max_chars=max(24, self._log_width() - 14))

    def _refresh_status_widgets(self) -> None:
        self.status_label.text = f"Status: {self.status}"
        self.stats_label.text = (
            f"Total: {self.total}\n"
            f"Processed: {self.processed}\n"
            f"Remaining: {self.remaining}\n"
            f"Failed: {self.failed}\n"
            f"Rate: {self.rate_per_hour:.2f} msgs/hour\n"
            f"ETA: {self.eta_text}"
        )
        percent = int(((self.processed + self.failed) / self.total) * 100) if self.total > 0 else 0
        bar_width = 28
        filled = int((percent / 100.0) * bar_width)
        bar = "#" * filled + "-" * (bar_width - filled)
        self.progress_label.text = f"Progress: [{bar}] {percent}%"
        completion = f"{self.completion_level}: {self.completion_message}"
        self.completion_label.text = self._truncate_for_log(completion, max_chars=max(24, self._log_width() - 14))

        self.start_button.text = "Start" if self._button_enabled["start"] else "[Start]"
        self.pause_button.text = "Pause" if self._button_enabled["pause"] else "[Pause]"
        self.resume_button.text = "Resume" if self._button_enabled["resume"] else "[Resume]"
        self.stop_button.text = "Stop" if self._button_enabled["stop"] else "[Stop]"

    def _append_log(self, level: str, message: str) -> None:
        line = self._format_log_line(level=level, message=message, max_width=self._log_width())
        self.logs.append(line)
        self.log_area.text = "\n".join(self.logs)
        self.log_area.buffer.cursor_position = len(self.log_area.text)

    def _log_width(self) -> int:
        try:
            columns = self.application.output.get_size().columns
        except Exception:
            columns = 120
        return max(50, columns - 8)

    def _save_tui_preferences(self) -> None:
        mode = str(self.mode_list.current_value or MODE_ALL)
        order = str(self.order_list.current_value or ORDER_NEWEST)
        try:
            self.service.store.save_setting(
                "tui_preferences",
                {
                    "mode": mode,
                    "order_direction": order,
                },
            )
        except Exception:
            return

    def _load_tui_preferences(self) -> None:
        try:
            payload = self.service.store.read_setting("tui_preferences") or {}
            mode = str(payload.get("mode", MODE_ALL))
            order = str(payload.get("order_direction", ORDER_NEWEST))
            if mode in {MODE_ALL, MODE_TEXT, MODE_LINKS, MODE_MEDIA}:
                self.mode_list.current_value = mode
            if order in {ORDER_NEWEST, ORDER_OLDEST}:
                self.order_list.current_value = order
        except Exception:
            self.mode_list.current_value = MODE_ALL
            self.order_list.current_value = ORDER_NEWEST

    @staticmethod
    def _truncate_for_log(message: str, max_chars: int) -> str:
        if max_chars < 1:
            return ""
        single_line = " ".join(str(message).replace("\n", " ").replace("\r", " ").split())
        if len(single_line) <= max_chars:
            return single_line
        if max_chars <= 3:
            return "." * max_chars
        return f"{single_line[: max_chars - 3]}..."

    @staticmethod
    def _format_fetch_error_breakdown(breakdown: dict[str, int]) -> str:
        if not breakdown:
            return "none"
        return ", ".join(f"{status}={count}" for status, count in sorted(breakdown.items()))

    def _format_log_line(self, *, level: str, message: str, max_width: int = 120) -> str:
        ts = datetime.now().strftime("%H:%M:%S")
        norm_level = level.upper().strip()
        max_message_chars = max(20, max_width - 22)
        safe_message = self._truncate_for_log(message, max_message_chars)
        return f"{ts} | {norm_level:<5} | {safe_message}"
