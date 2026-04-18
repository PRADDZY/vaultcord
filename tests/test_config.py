import json
from pathlib import Path

import pytest

from vaultcord import config


def test_load_config_expands_user_path(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    monkeypatch.setattr(config.Path, "home", staticmethod(lambda: fake_home))
    cfg_path = config.ensure_config()
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert raw["data_dir"] == str(fake_home / ".vaultcord")
    assert raw["batch_prepare_size"] == 1000


def test_expand_user_from_custom_config(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home2"
    fake_home.mkdir()

    monkeypatch.setattr(config.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    cfg_path = config.default_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        """
{
  "data_dir": "~/.vaultcord",
  "db_path": "~/.vaultcord/vaultcord.db",
  "log_path": "~/.vaultcord/vaultcord.log",
  "request_timeout_seconds": 20.0,
  "max_retries": 3,
  "batch_prepare_size": 1000,
  "scheduler": {
    "edit_delay_min_seconds": 15,
    "edit_delay_max_seconds": 25,
    "run_hours_min": 1.5,
    "run_hours_max": 3.0,
    "pause_hours_min": 0.5,
    "pause_hours_max": 2.0
  }
}
""".strip(),
        encoding="utf-8",
    )

    loaded = config.load_config()
    assert loaded.data_dir.startswith(str(fake_home))
    assert loaded.db_path.startswith(str(fake_home))
    assert loaded.log_path.startswith(str(fake_home))


def test_invalid_scheduler_bounds_raise(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home3"
    fake_home.mkdir()

    monkeypatch.setattr(config.Path, "home", staticmethod(lambda: fake_home))
    cfg_path = config.default_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        """
{
  "data_dir": "~/.vaultcord",
  "db_path": "~/.vaultcord/vaultcord.db",
  "log_path": "~/.vaultcord/vaultcord.log",
  "request_timeout_seconds": 20.0,
  "max_retries": 3,
  "batch_prepare_size": 1000,
  "scheduler": {
    "edit_delay_min_seconds": 30,
    "edit_delay_max_seconds": 10,
    "run_hours_min": 1.5,
    "run_hours_max": 3.0,
    "pause_hours_min": 0.5,
    "pause_hours_max": 2.0
  }
}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        config.load_config()


def test_invalid_batch_size_raises(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home4"
    fake_home.mkdir()

    monkeypatch.setattr(config.Path, "home", staticmethod(lambda: fake_home))
    cfg_path = config.default_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        """
{
  "data_dir": "~/.vaultcord",
  "db_path": "~/.vaultcord/vaultcord.db",
  "log_path": "~/.vaultcord/vaultcord.log",
  "request_timeout_seconds": 20.0,
  "max_retries": 3,
  "batch_prepare_size": 0,
  "scheduler": {
    "edit_delay_min_seconds": 15,
    "edit_delay_max_seconds": 25,
    "run_hours_min": 1.5,
    "run_hours_max": 3.0,
    "pause_hours_min": 0.5,
    "pause_hours_max": 2.0
  }
}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        config.load_config()
