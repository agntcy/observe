# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import os
import threading


class _RealtimeObservabilityConfig:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._override: bool | None = None

    def get_override(self) -> bool | None:
        with self._lock:
            return self._override

    def set_override(self, enabled: bool | None) -> None:
        with self._lock:
            self._override = enabled


_realtime_observability_config = _RealtimeObservabilityConfig()


def is_tracing_enabled() -> bool:
    return (os.getenv("OBSERVE_TRACING_ENABLED") or "true").lower() == "true"


def is_content_tracing_enabled() -> bool:
    return (os.getenv("OBSERVE_TRACE_CONTENT") or "true").lower() == "true"


def is_metrics_enabled() -> bool:
    return (os.getenv("OBSERVE_METRICS_ENABLED") or "true").lower() == "true"


def is_logging_enabled() -> bool:
    return (os.getenv("OBSERVE_LOGGING_ENABLED") or "false").lower() == "true"


def is_realtime_observability_enabled() -> bool:
    override = _realtime_observability_config.get_override()
    if override is not None:
        return override
    return (
        os.getenv("OBSERVE_REALTIME_OBSERVABILITY_ENABLED") or "true"
    ).lower() == "true"


def set_realtime_observability_enabled(enabled: bool | None) -> None:
    _realtime_observability_config.set_override(enabled)
