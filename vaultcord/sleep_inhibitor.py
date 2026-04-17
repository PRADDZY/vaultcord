"""Best-effort cross-platform sleep inhibition while jobs run."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional


class SleepInhibitor:
    def __init__(self) -> None:
        self._platform = sys.platform
        self._mac_process: Optional[subprocess.Popen[str]] = None
        self._linux_process: Optional[subprocess.Popen[str]] = None

    def acquire(self) -> tuple[bool, str]:
        if self._platform.startswith("win"):
            return self._acquire_windows()
        if self._platform == "darwin":
            return self._acquire_macos()
        return self._acquire_linux()

    def release(self) -> None:
        if self._platform.startswith("win"):
            self._release_windows()
            return
        if self._platform == "darwin":
            self._terminate_process(self._mac_process)
            self._mac_process = None
            return
        self._terminate_process(self._linux_process)
        self._linux_process = None

    def _acquire_windows(self) -> tuple[bool, str]:
        try:
            import ctypes

            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ES_AWAYMODE_REQUIRED = 0x00000040
            result = ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
            )
            if result == 0:
                return False, "SetThreadExecutionState returned 0"
            return True, "Windows sleep inhibition active"
        except Exception as exc:  # pragma: no cover - defensive
            return False, f"Windows sleep inhibition unavailable: {type(exc).__name__}"

    def _release_windows(self) -> None:
        try:
            import ctypes

            ES_CONTINUOUS = 0x80000000
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:
            return

    def _acquire_macos(self) -> tuple[bool, str]:
        try:
            self._mac_process = subprocess.Popen(
                ["caffeinate", "-dimsu"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            return True, "macOS caffeinate active"
        except Exception as exc:  # pragma: no cover - env-dependent
            return False, f"macOS sleep inhibition unavailable: {type(exc).__name__}"

    def _acquire_linux(self) -> tuple[bool, str]:
        # Best-effort path: systemd-inhibit process that holds an inhibitor lock.
        try:
            self._linux_process = subprocess.Popen(
                [
                    "systemd-inhibit",
                    "--what=sleep",
                    "--why=VaultCord running",
                    "--mode=block",
                    "sleep",
                    "infinity",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                env=os.environ.copy(),
            )
            return True, "Linux systemd-inhibit active"
        except Exception as exc:  # pragma: no cover - env-dependent
            return False, f"Linux sleep inhibition unavailable: {type(exc).__name__}"

    @staticmethod
    def _terminate_process(proc: Optional[subprocess.Popen[str]]) -> None:
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()
