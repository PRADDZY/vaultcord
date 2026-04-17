"""Runtime helpers for app construction."""

from __future__ import annotations

from dataclasses import dataclass

from .config import load_config
from .logging_utils import configure_logging
from .service import VaultService
from .storage import VaultStore


@dataclass(slots=True)
class RuntimeContext:
    config: object
    store: VaultStore
    service: VaultService


def build_runtime() -> RuntimeContext:
    config = load_config()
    configure_logging(config.log_path)
    store = VaultStore(config.db_path)
    service = VaultService(config=config, store=store)
    return RuntimeContext(config=config, store=store, service=service)
