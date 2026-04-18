from pathlib import Path
from unittest.mock import patch

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


def test_tui_has_interactive_controls(tmp_path: Path) -> None:
    app = build_tui(tmp_path)
    assert app.guild_input is not None
    assert app.start_button is not None
    assert app.pause_button is not None
    assert app.resume_button is not None
    assert app.stop_button is not None
    assert app.mode_list.current_value == "all"
    assert app.order_list.current_value == "newest"


def test_tui_persists_mode_and_order_preferences(tmp_path: Path) -> None:
    app = build_tui(tmp_path)
    app.mode_list.current_value = MODE_LINKS
    app.order_list.current_value = ORDER_OLDEST
    app._save_tui_preferences()

    restored = build_tui(tmp_path)
    assert restored.mode_list.current_value == MODE_LINKS
    assert restored.order_list.current_value == ORDER_OLDEST


def test_tui_shows_success_completion_banner(tmp_path: Path) -> None:
    app = build_tui(tmp_path)
    app._handle_completed(
        {
            "done": 93,
            "failed": 0,
            "remaining": 0,
            "elapsed_seconds": 120,
        }
    )
    assert "all queued messages archived and replaced" in app.completion_message
    assert app.completion_level == "OK"
    assert app.status == "Completed"


def test_log_line_truncates_and_keeps_columns(tmp_path: Path) -> None:
    app = build_tui(tmp_path)
    line = app._format_log_line(level="INFO", message="x" * 500, max_width=68)
    assert line.count("|") == 2
    assert "\n" not in line
    assert len(line) <= 68


def test_status_transitions_control_buttons(tmp_path: Path) -> None:
    app = build_tui(tmp_path)
    app._set_status("running")
    assert app._button_enabled["pause"]
    assert app._button_enabled["stop"]
    assert not app._button_enabled["start"]
    app._set_status("paused")
    assert app._button_enabled["resume"]
    assert app._button_enabled["stop"]
    assert not app._button_enabled["pause"]


def test_tui_can_insert_text_into_focused_inputs(tmp_path: Path) -> None:
    app = build_tui(tmp_path)
    with patch.object(
        app.application.layout, "has_focus", side_effect=lambda widget: widget is app.guild_input
    ):
        app._insert_into_focused_input("123456789")
    assert app.guild_input.text.endswith("123456789")

    with patch.object(
        app.application.layout, "has_focus", side_effect=lambda widget: widget is app.vault_id_input
    ):
        app._insert_into_focused_input("vault://abc")
    assert app.vault_id_input.text.endswith("vault://abc")
