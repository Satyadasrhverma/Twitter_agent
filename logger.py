"""
Logging setup: rotating file handler + rich-coloured console output.
Call setup_logging() once at startup; then use logging.getLogger(__name__).
"""

import logging
import logging.handlers
import os

from rich.logging import RichHandler

import config


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logger with console (Rich) and rotating file handlers."""

    os.makedirs(config.LOG_DIR, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    # Prevent duplicate handlers if called more than once
    if root.handlers:
        return

    # --- Console handler (Rich coloured output) ---
    console_handler = RichHandler(
        rich_tracebacks=True,
        markup=True,
        show_time=True,
        show_path=False,
    )
    console_handler.setLevel(level)
    console_fmt = logging.Formatter("%(message)s", datefmt="[%X]")
    console_handler.setFormatter(console_fmt)

    # --- Rotating file handler ---
    file_handler = logging.handlers.RotatingFileHandler(
        filename=config.LOG_FILE,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_fmt)

    root.addHandler(console_handler)
    root.addHandler(file_handler)
