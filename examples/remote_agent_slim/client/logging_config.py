# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

"""
This module provides logging configuration for the remote graphs client.
It sets up structured JSON logging with rotation and supports logging to both console and file.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pythonjsonlogger.json import JsonFormatter


def get_log_dir() -> Path:
    """Returns the log directory path and ensures it exists."""
    log_dir = Path.cwd() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def get_log_level() -> str:
    """Retrieves the log level from environment variables (defaults to INFO)."""
    return os.getenv("LOG_LEVEL", "INFO").upper()


def configure_logging() -> logging.Logger:
    """
    Configures structured JSON logging with rotation.

    Logs to both console and a rotating file handler.
    The log level is determined by the LOG_LEVEL environment variable.
    """
    log_dir = get_log_dir()
    log_file = log_dir / "ap_rest_client.log"
    log_level: str = get_log_level()

    logger = logging.getLogger()
    logger.setLevel(log_level)

    formatter = JsonFormatter(
        "{asctime} {levelname} {pathname} {module} {funcName} {message} {exc_info}",
        style="{",
    )

    # ✅ Log to Console
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    # ✅ Log to File with Rotation
    file_handler = RotatingFileHandler(
        log_file, mode="a", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.info(
        "Logging initialized with rotation.", extra={"log_destination": str(log_file)}
    )

    return logger
