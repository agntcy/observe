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


def _read_positive_float_env(name: str, default: float | None) -> float | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else None


def _read_positive_int_env(name: str, default: int | None) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else None


def realtime_session_ttl_seconds() -> float | None:
    """Idle timeout after which an inactive session's live state is evicted.

    Returns ``None`` to disable TTL-based eviction. Defaults to 1 hour.
    """
    return _read_positive_float_env(
        "OBSERVE_REALTIME_SESSION_TTL_SECONDS",
        3600.0,
    )


def realtime_max_sessions() -> int | None:
    """Maximum number of live sessions kept in memory before evicting the oldest.

    Returns ``None`` to disable the cap. Defaults to 1000 sessions.
    """
    return _read_positive_int_env(
        "OBSERVE_REALTIME_MAX_SESSIONS",
        1000,
    )
