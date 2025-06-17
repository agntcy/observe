# SPDX-FileCopyrightText: Copyright (c) 2025 Cisco and/or its affiliates.
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import logging.config
import os
import traceback
from pathlib import Path
from typing import Dict


class JSONFormatter(logging.Formatter):
    """
    Custom logging formatter that outputs logs in structured JSON format.

    - Includes timestamp, log level, message, module, function, and line number.
    - Captures exceptions with stack trace when applicable.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": self.formatTime(
                record
            ),  # Corrected: Uses default ISO 8601 format
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "logger": record.name,
            "pid": record.process,
        }

        # Capture exception details if they exist
        if record.exc_info:
            log_data["error"] = {
                "type": str(record.exc_info[0]),
                "message": str(record.exc_info[1]),
                "stack_trace": traceback.format_exc(),
            }

        return json.dumps(log_data)


def get_log_dir() -> Path:
    """
    Returns the log directory path and ensures it exists.

    Returns:
        Path: The path to the logs directory (app/logs/)
    """
    log_dir = Path(__file__).parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def get_log_file() -> Path:
    """
    Returns the log file path and ensures it is removed on every startup.

    Returns:
        Path: The path to the log file (app/logs/remote_graphs.log)
    """
    log_file = get_log_dir() / "remote_graphs.log"

    # Remove old log file on startup to ensure fresh logs
    if log_file.exists():
        log_file.unlink()

    return log_file


def get_log_level() -> str:
    """
    Retrieves the log level from environment variables.

    Defaults to "INFO" if LOG_LEVEL is not set.

    Returns:
        str: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """
    return os.getenv("LOG_LEVEL", "INFO").upper()


def get_logging_config(log_file: Path, log_level: str) -> Dict:
    """
    Generates the logging configuration dictionary with JSON formatting.

    Args:
        log_file (Path): Path to the log file.
        log_level (str): Logging level (DEBUG, INFO, etc.).

    Returns:
        dict: Logging configuration dictionary.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": JSONFormatter,  # Use custom JSON formatter
            },
        },
        "handlers": {
            "console": {
                "level": log_level,
                "class": "logging.StreamHandler",
                "formatter": "json",
            },
            "file": {
                "level": log_level,
                "class": "logging.FileHandler",
                "filename": str(log_file),
                "formatter": "json",
            },
        },
        "loggers": {
            "remote_agent_slim": {  # Keep for compatibility
                "handlers": ["console", "file"],
                "level": log_level,
                "propagate": False,
            },
        },
        "root": {"handlers": ["console", "file"], "level": log_level},
    }


def configure_logging() -> None:
    """
    Configures structured JSON logging for the FastAPI application.

    - Initializes logging configuration.
    - Removes old log files to ensure clean logs on every restart.
    - Applies JSON formatting globally.
    """
    log_file = get_log_file()
    log_level = get_log_level()
    logging_config = get_logging_config(log_file, log_level)

    logging.config.dictConfig(logging_config)

    logger = logging.getLogger("app")
    logger.info("Logging is initialized. This should appear in the log file.")
