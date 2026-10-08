# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import Span, SpanContext

from ioa_observe.sdk.config import is_realtime_observability_enabled
from ioa_observe.sdk.tracing.runtime_events import span_correlation_attributes


RuntimeEventListener = Callable[[dict[str, Any]], None]

_logger = logging.getLogger(__name__)
_listeners: list[RuntimeEventListener] = []
_listeners_lock = threading.RLock()
_runtime_logger_lock = threading.RLock()
_runtime_logger_ready = False


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


def emit_runtime_event(
    attributes: Mapping[str, str | bool | int | float],
    *,
    span: Span | ReadableSpan | None = None,
) -> None:
    if not is_realtime_observability_enabled():
        return
    if span is None:
        span = trace.get_current_span()
    span_context = span.get_span_context()
    correlation = span_correlation_attributes(
        getattr(span, "attributes", None) or {},
        trace_id=span_context.trace_id,
        span_id=span_context.span_id,
    )
    event = dict(attributes)
    event.update(correlation)
    _emit_to_listeners(event)
    _emit_to_otel_logs(event, span_context)


def _emit_to_listeners(event: dict[str, Any]) -> None:
    with _listeners_lock:
        listeners = tuple(_listeners)

    for listener in listeners:
        try:
            listener(dict(event))
        except Exception:  # pragma: no cover - listener isolation
            _logger.exception("Runtime event listener failed")


def _emit_to_otel_logs(event: dict[str, Any], span_context: SpanContext) -> None:
    try:
        _ensure_runtime_event_logger()
        from opentelemetry._logs import LogRecord, SeverityNumber, get_logger

        logger = get_logger("ioa_observe.runtime_events")
        observed_timestamp = int(time.time() * 1_000_000_000)
        record = LogRecord(
            timestamp=observed_timestamp,
            observed_timestamp=observed_timestamp,
            trace_id=span_context.trace_id,
            span_id=span_context.span_id,
            trace_flags=span_context.trace_flags,
            severity_text="INFO",
            severity_number=SeverityNumber.INFO,
            body=event.get("event.name", "runtime.event"),
            attributes=event,
            event_name=event.get("event.name", "runtime.event"),
        )
        logger.emit(record)
    except Exception:  # pragma: no cover - depends on installed OTel log API
        _logger.debug("Unable to emit runtime event through OTel logs", exc_info=True)


def _ensure_runtime_event_logger() -> None:
    global _runtime_logger_ready

    if _runtime_logger_ready:
        return

    with _runtime_logger_lock:
        if _runtime_logger_ready:
            return

        from opentelemetry._logs import get_logger_provider

        current_provider = get_logger_provider()
        if type(current_provider).__name__ != "ProxyLoggerProvider":
            _runtime_logger_ready = True
            return

        from ioa_observe.sdk.logging.logging import (
            init_logging_exporter,
            init_logging_provider,
        )
        from ioa_observe.sdk.tracing.tracing import TracerWrapper

        if not TracerWrapper.endpoint:
            return

        exporter = init_logging_exporter(TracerWrapper.endpoint, TracerWrapper.headers)
        init_logging_provider(
            exporter,
            TracerWrapper.resource_attributes,
            install_logging_handler=False,
            # Export runtime events immediately so downstream materializers can
            # observe live session state before the matching spans flush.
            use_simple_processor=True,
        )
        _runtime_logger_ready = True
