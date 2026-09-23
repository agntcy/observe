# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import threading
from typing import Any

from ioa_observe.sdk.client import kv_store


@dataclass(frozen=True)
class HandoffSignal:
    target_agent: str
    evidence: str
    confidence: float


HandoffExtractor = Callable[[Any], HandoffSignal | None]
_extractors: list[HandoffExtractor] = []
_pending_lock = threading.RLock()


def register_handoff_extractor(extractor: HandoffExtractor) -> None:
    if extractor not in _extractors:
        _extractors.append(extractor)


def extract_handoff_signal(result: Any) -> HandoffSignal | None:
    _register_builtin_extractors()
    for extractor in _extractors:
        signal = extractor(result)
        if signal is not None:
            return signal
    return None


def record_handoff_signal(
    session_id: str,
    source_agent: str,
    source_span_id: str,
    source_trace_id: str,
    signal: HandoffSignal,
) -> None:
    key = _pending_key(session_id, signal.target_agent)
    value = json.dumps(
        {
            "source_agent": source_agent,
            "source_span_id": source_span_id,
            "source_trace_id": source_trace_id,
            "evidence": signal.evidence,
            "confidence": signal.confidence,
        }
    )
    with _pending_lock:
        kv_store.set(key, value)


def consume_handoff_signal(session_id: str, target_agent: str) -> dict[str, Any] | None:
    key = _pending_key(session_id, target_agent)
    with _pending_lock:
        value = kv_store.get(key)
        if value is None:
            return None
        kv_store.delete(key)
    return json.loads(value)


def _pending_key(session_id: str, target_agent: str) -> str:
    return f"session.{session_id}.pending_handoff.{target_agent}"


def _register_builtin_extractors() -> None:
    from ioa_observe.sdk.instrumentations.langgraph import extract_langgraph_handoff

    register_handoff_extractor(extract_langgraph_handoff)
