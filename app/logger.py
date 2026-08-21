"""Central application logging configuration."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from app.config import get_settings


def configure_logging() -> logging.Logger:
    settings = get_settings()
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("youtube_ai_pipeline")
    if logger.handlers:
        return logger

    level = getattr(logging, settings.log_level, logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    file_handler = RotatingFileHandler(
        settings.log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    root_logger = configure_logging()
    return root_logger if not name else root_logger.getChild(name)

