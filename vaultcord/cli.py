"""Typer CLI entrypoint."""

from __future__ import annotations

import asyncio
import signal
from datetime import timedelta
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from .constants import MODE_ALL, ORDER_NEWEST, VALID_MODES, VALID_ORDER_DIRECTIONS
from .logging_utils import suppress_console_logging
from .runtime import build_runtime
from .tui import VaultCordTUI
from .worker import ScrubWorker, WorkerControl

app = typer.Typer(help="VaultCord - local encrypted Discord message vault")
console = Console()


def _check_mode(mode: str) -> str:
    if mode not in VALID_MODES:
        raise typer.BadParameter(f"mode must be one of: {sorted(VALID_MODES)}")
    return mode


def _check_order(order: str) -> str:
    value = order.lower()
    if value not in VALID_ORDER_DIRECTIONS:
        raise typer.BadParameter(f"order must be one of: {sorted(VALID_ORDER_DIRECTIONS)}")
    return value


def _warn_token_sensitivity() -> None:
    console.print(
        "[bold yellow]Warning:[/bold yellow] Your Discord token is sensitive. "
        "Use VaultCord only on systems you control. Token misuse is your responsibility."
    )


def _exit_with_error(message: str) -> None:
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=1)


def _format_progress(progress: dict[str, Any]) -> str:
    total = int(progress.get("total", 0))
    done = int(progress.get("done", 0))
    failed = int(progress.get("failed", 0))
    remaining = int(progress.get("remaining", 0))
    elapsed = int(progress.get("elapsed_seconds", 0))

    rate = 0.0
    if elapsed > 0:
        rate = (done / elapsed) * 3600

    eta_text = "--"
    if rate > 0 and remaining > 0:
        eta_seconds = int((remaining / rate) * 3600)
        eta_text = str(timedelta(seconds=eta_seconds))

    return (
        f"total={total} done={done} failed={failed} remaining={remaining} "
        f"rate={rate:.2f}/h eta={eta_text}"
    )


@app.command()
def login() -> None:
    """Save encrypted Discord token after validation."""
    try:
        runtime = build_runtime()
        _warn_token_sensitivity()

        token = typer.prompt("Discord token", hide_input=True)
        password = typer.prompt("Vault password", hide_input=True, confirmation_prompt=True)

        result = asyncio.run(runtime.service.login(token=token, password=password))
        console.print(f"[green]Login saved for {result['username']} ({result['user_id']})[/green]")
    except Exception:
        _exit_with_error("Login failed. Check token/password and try again.")


@app.command()
def scrub(
    guild_id: str = typer.Option(..., "--guild-id", help="Discord guild/server ID"),
    mode: str = typer.Option(MODE_ALL, "--mode", callback=lambda v: _check_mode(v.lower())),
    order: str = typer.Option(ORDER_NEWEST, "--order", callback=_check_order),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview counts without editing"),
) -> None:
    """Scrape authored messages, archive encrypted, and replace on Discord."""
    try:
        runtime = build_runtime()
        password = typer.prompt("Vault password", hide_input=True)
        session = runtime.service.unlock_session(password)

        asyncio.run(runtime.service.validate_session(session))

        if dry_run:
            counts = asyncio.run(runtime.service.preview_counts(session, guild_id))
            table = Table(title="Dry Run Counts")
            table.add_column("Mode")
            table.add_column("Count", justify="right")
            for key in ("all", "text", "links", "media"):
                table.add_row(key, str(counts.get(key, 0)))
            console.print(table)
            return

        if runtime.service.has_retryable_queue(guild_id=guild_id, mode=mode):
            console.print("[cyan]Resume detected:[/cyan] using existing queued work before re-scan")
        else:
            prepare = asyncio.run(
                runtime.service.prepare_jobs(
                    session,
                    guild_id=guild_id,
                    mode=mode,
                    order_direction=order,
                )
            )
            console.print(
                "[cyan]Queue prepared:[/cyan] "
                f"queued={prepare.queued} skipped={prepare.skipped} already_ref={prepare.already_referenced}"
            )

        worker = ScrubWorker(
            store=runtime.store,
            session=session,
            scheduler=runtime.config.scheduler,
            request_timeout_seconds=runtime.config.request_timeout_seconds,
            max_retries=runtime.config.max_retries,
        )

        control = WorkerControl()

        def _signal_handler(*_: object) -> None:
            control.stop_event.set()

        signal.signal(signal.SIGINT, _signal_handler)

        async def _run() -> None:
            await worker.run(
                guild_id=guild_id,
                mode=mode,
                retry_failed_only=False,
                order_direction=order,
                control=control,
                event_sink=_event_sink,
            )

        def _event_sink(event: dict[str, Any]) -> None:
            event_type = event.get("type")
            if event_type == "log":
                console.print(f"[{event.get('level', 'INFO')}] {event.get('message', '')}")
            elif event_type == "progress":
                console.print(_format_progress(event))
            elif event_type == "completed":
                console.print(
                    "completed "
                    f"done={event.get('done', 0)} "
                    f"failed={event.get('failed', 0)} "
                    f"remaining={event.get('remaining', 0)} "
                    f"elapsed={event.get('elapsed_seconds', 0)}s"
                )
            elif event_type == "status":
                console.print(f"status={event.get('status')}")

        asyncio.run(_run())
    except Exception:
        _exit_with_error("Scrub failed. Review logs and retry with the same vault password.")


@app.command("retry-failed")
def retry_failed(
    guild_id: str = typer.Option(..., "--guild-id", help="Discord guild/server ID"),
    mode: str = typer.Option(MODE_ALL, "--mode", callback=lambda v: _check_mode(v.lower())),
    order: str = typer.Option(ORDER_NEWEST, "--order", callback=_check_order),
) -> None:
    """Reset failed jobs and retry them."""
    try:
        runtime = build_runtime()
        password = typer.prompt("Vault password", hide_input=True)
        session = runtime.service.unlock_session(password)

        asyncio.run(runtime.service.validate_session(session))

        reset_count = runtime.service.retry_failed(guild_id=guild_id, mode=mode)
        console.print(f"Reset {reset_count} failed jobs")

        worker = ScrubWorker(
            store=runtime.store,
            session=session,
            scheduler=runtime.config.scheduler,
            request_timeout_seconds=runtime.config.request_timeout_seconds,
            max_retries=runtime.config.max_retries,
        )
        control = WorkerControl()

        def _signal_handler(*_: object) -> None:
            control.stop_event.set()

        signal.signal(signal.SIGINT, _signal_handler)

        def _event_sink(event: dict[str, Any]) -> None:
            event_type = event.get("type")
            if event_type == "log":
                console.print(f"[{event.get('level', 'INFO')}] {event.get('message', '')}")
            elif event_type == "progress":
                console.print(_format_progress(event))
            elif event_type == "completed":
                console.print(
                    "completed "
                    f"done={event.get('done', 0)} "
                    f"failed={event.get('failed', 0)} "
                    f"remaining={event.get('remaining', 0)} "
                    f"elapsed={event.get('elapsed_seconds', 0)}s"
                )
            elif event_type == "status":
                console.print(f"status={event.get('status')}")

        async def _run() -> None:
            await worker.run(
                guild_id=guild_id,
                mode=mode,
                retry_failed_only=True,
                order_direction=order,
                control=control,
                event_sink=_event_sink,
            )

        asyncio.run(_run())
    except Exception:
        _exit_with_error("Retry-failed run could not complete. Review logs and try again.")


@app.command()
def get(vault_id: str) -> None:
    """Decrypt and display a vaulted message by vault ID."""
    try:
        runtime = build_runtime()
        password = typer.prompt("Vault password", hide_input=True)
        data = runtime.service.decrypt_vault_message(vault_id=vault_id, password=password)

        table = Table(title=f"Vault Message {vault_id}")
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("message_id", str(data.get("message_id", "")))
        table.add_row("channel_id", str(data.get("channel_id", "")))
        table.add_row("timestamp", str(data.get("timestamp", "")))
        table.add_row("content", str(data.get("content", "")))
        table.add_row("attachments", str(len(data.get("attachments", []))))
        console.print(table)
    except Exception:
        _exit_with_error("Unable to retrieve vault message. Check vault id and password.")


@app.command()
def tui() -> None:
    """Launch interactive prompt-toolkit dashboard."""
    try:
        runtime = build_runtime()
        password = typer.prompt("Vault password", hide_input=True)
        session = runtime.service.unlock_session(password)
        app_ui = VaultCordTUI(service=runtime.service, session=session, config=runtime.config)
        with suppress_console_logging():
            asyncio.run(runtime.service.validate_session(session))
            app_ui.run()
    except Exception as exc:
        _exit_with_error(
            f"Unable to launch TUI ({type(exc).__name__}). Validate credentials and local config."
        )


if __name__ == "__main__":
    app()
