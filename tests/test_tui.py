from pathlib import Path

import pytest
from textual.widgets import Input, RadioButton, Static

from vaultcord.constants import MODE_LINKS, ORDER_OLDEST
from vaultcord.models import AppConfig, SchedulerConfig, VaultSession
from vaultcord.service import VaultService
from vaultcord.storage import VaultStore
from vaultcord.tui import VaultCordTUI


def build_tui(tmp_path: Path) -> VaultCordTUI:
    config = AppConfig(
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "vault.db"),
        log_path=str(tmp_path / "vault.log"),
        request_timeout_seconds=20.0,
        max_retries=3,
        scheduler=SchedulerConfig(),
    )
    service = VaultService(config=config, store=VaultStore(config.db_path))
    session = VaultSession(user_id="u1", username="user#0001", token="token", password="pw")
    return VaultCordTUI(service=service, session=session, config=config)


@pytest.mark.asyncio
async def test_tui_focuses_server_id_on_mount(tmp_path: Path) -> None:
    app = build_tui(tmp_path)
    async with app.run_test():
        guild_input = app.query_one("#guild-id", Input)
        assert app.focused is guild_input


@pytest.mark.asyncio
async def test_tui_persists_mode_and_order_preferences(tmp_path: Path) -> None:
    app = build_tui(tmp_path)
    async with app.run_test():
        app.selected_mode = MODE_LINKS
        app.selected_order = ORDER_OLDEST
        app._save_tui_preferences()

    restored = build_tui(tmp_path)
    async with restored.run_test():
        assert restored.selected_mode == MODE_LINKS
        assert restored.selected_order == ORDER_OLDEST
        assert restored.query_one("#mode-links", RadioButton).value
        assert restored.query_one("#order-oldest", RadioButton).value


@pytest.mark.asyncio
async def test_tui_shows_success_completion_banner(tmp_path: Path) -> None:
    app = build_tui(tmp_path)
    async with app.run_test():
        app._handle_completed(
            {
                "done": 93,
                "failed": 0,
                "remaining": 0,
                "elapsed_seconds": 120,
            }
        )
        banner = app.query_one("#completion-banner", Static)
        assert "all queued messages archived and replaced" in str(banner.render())
        assert banner.has_class("is-success")
        assert "Completed" in str(app.query_one("#status-chip", Static).render())


def test_render_log_truncates_and_keeps_columns(tmp_path: Path) -> None:
    app = build_tui(tmp_path)
    line = app._render_log("INFO", "x" * 500, max_width=68)
    assert line.plain.count("|") == 2
    assert "\n" not in line.plain
    assert len(line.plain) <= 68
