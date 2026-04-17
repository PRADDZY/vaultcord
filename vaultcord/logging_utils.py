"""Structured logging configuration."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class SensitiveFilter(logging.Filter):
    """Basic filter to prevent obvious token leaks in logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage())
        if "Authorization" in msg or "token" in msg.lower():
            record.msg = "[redacted sensitive log entry]"
            record.args = ()
        return True


def configure_logging(log_path: str) -> None:
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    if root.handlers:
        return

    formatter = logging.Formatter(_LOG_FORMAT)
    file_handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(SensitiveFilter())

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(SensitiveFilter())

    root.addHandler(file_handler)
    root.addHandler(console_handler)
