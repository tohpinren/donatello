"""Logging setup for local and remote experiment runs."""

from __future__ import annotations

import logging
from pathlib import Path


def configure_logging(log_file: Path | None = None, *, verbose: bool = False) -> None:
    """Configure root logging for CLI commands."""

    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
