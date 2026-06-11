# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any


RuntimeEventListener = Callable[[dict[str, Any]], None]

_logger = logging.getLogger(__name__)
_listeners: list[RuntimeEventListener] = []
_listeners_lock = threading.RLock()


def register_runtime_event_listener(listener: RuntimeEventListener) -> None:
    with _listeners_lock:
        if listener not in _listeners:
            _listeners.append(listener)


def unregister_runtime_event_listener(listener: RuntimeEventListener) -> None:
    with _listeners_lock:
        if listener in _listeners:
            _listeners.remove(listener)


def clear_runtime_event_listeners() -> None:
    with _listeners_lock:
        _listeners.clear()


def emit_runtime_event(attributes: Mapping[str, str | bool | int | float]) -> None:
    event = dict(attributes)
    _emit_to_listeners(event)
    _emit_to_otel_logs(event)


def _emit_to_listeners(event: dict[str, Any]) -> None:
    with _listeners_lock:
        listeners = tuple(_listeners)

    for listener in listeners:
        try:
            listener(dict(event))
        except Exception:  # pragma: no cover - listener isolation
            _logger.exception("Runtime event listener failed")


def _emit_to_otel_logs(event: dict[str, Any]) -> None:
    try:
        from opentelemetry._logs import LogRecord, SeverityNumber, get_logger

        logger = get_logger("ioa_observe.runtime_events")
        observed_timestamp = int(time.time() * 1_000_000_000)
        record = LogRecord(
            timestamp=observed_timestamp,
            observed_timestamp=observed_timestamp,
            severity_text="INFO",
            severity_number=SeverityNumber.INFO,
            body=event.get("event.name", "runtime.event"),
            attributes=event,
            event_name=event.get("event.name", "runtime.event"),
        )
        logger.emit(record)
    except Exception:  # pragma: no cover - depends on installed OTel log API
        _logger.debug("Unable to emit runtime event through OTel logs", exc_info=True)
