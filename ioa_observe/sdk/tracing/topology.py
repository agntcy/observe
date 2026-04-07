# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote, unquote

from opentelemetry import trace
from opentelemetry.context import get_value
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from ioa_observe.sdk.client import kv_store

logger = logging.getLogger(__name__)

TopologyListener = Callable[[Dict[str, Any]], None]

_listeners: set[TopologyListener] = set()
_listener_lock = threading.RLock()
_snapshot_lock = threading.RLock()


def _node_key(session_id: str, node_id: str) -> str:
    return f"session.{session_id}.topology.nodes.{quote(str(node_id), safe='')}"


def _edge_key(session_id: str, source: str, target: str, transport: str) -> str:
    encoded = quote(f"{transport}:{source}->{target}", safe="")
    return f"session.{session_id}.topology.edges.{encoded}"


def _version_key(session_id: str) -> str:
    return f"session.{session_id}.topology.version"


def _load_json(key: str) -> Optional[dict]:
    raw = kv_store.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _store_json(key: str, value: dict) -> None:
    kv_store.set(key, json.dumps(value, sort_keys=True))


def _next_snapshot_version(session_id: str) -> int:
    with _snapshot_lock:
        current = kv_store.get(_version_key(session_id))
        version = int(current) + 1 if current else 1
        kv_store.set(_version_key(session_id), str(version))
        return version


def _get_snapshot_version(session_id: str) -> int:
    current = kv_store.get(_version_key(session_id))
    return int(current) if current else 0


def _get_current_traceparent() -> Optional[str]:
    stored_traceparent = get_value("current_traceparent")
    if stored_traceparent:
        return stored_traceparent

    current_span = trace.get_current_span()
    if current_span.is_recording():
        carrier = {}
        TraceContextTextMapPropagator().inject(carrier)
        return carrier.get("traceparent")

    return None


def _trace_id_from_traceparent(traceparent: Optional[str]) -> Optional[str]:
    if not traceparent:
        return None
    parts = str(traceparent).split("-")
    if len(parts) != 4:
        return None
    return parts[1] or None


def _get_current_session_id() -> Optional[str]:
    session_id = get_value("session.id")
    if session_id:
        return str(session_id)

    traceparent = _get_current_traceparent()
    if traceparent:
        stored = kv_store.get(f"execution.{traceparent}")
        if stored:
            return stored
    return None


def get_current_topology_context(actor: Optional[str] = None) -> dict:
    traceparent = _get_current_traceparent()
    session_id = _get_current_session_id()
    return {
        "traceparent": traceparent,
        "trace_id": _trace_id_from_traceparent(traceparent),
        "session_id": session_id,
        "actor": actor or get_value("workflow_name"),
        "timestamp_ms": int(time.time() * 1000),
    }


def register_topology_listener(listener: TopologyListener) -> None:
    if not callable(listener):
        raise TypeError("topology listener must be callable")
    with _listener_lock:
        _listeners.add(listener)


def unregister_topology_listener(listener: TopologyListener) -> None:
    with _listener_lock:
        _listeners.discard(listener)


def clear_topology_listeners() -> None:
    with _listener_lock:
        _listeners.clear()


def _dispatch_event(event: dict) -> None:
    with _listener_lock:
        listeners = list(_listeners)

    for listener in listeners:
        try:
            listener(event)
        except Exception:
            logger.exception("topology listener failed for event %s", event.get("type"))


def get_live_topology_snapshot(session_id: Optional[str]) -> dict:
    if not session_id:
        return {"session_id": None, "version": 0, "nodes": [], "edges": []}

    node_prefix = f"session.{session_id}.topology.nodes."
    edge_prefix = f"session.{session_id}.topology.edges."

    nodes = []
    for key in kv_store.get_keys_by_prefix(node_prefix):
        node = _load_json(key)
        if node:
            if "id" not in node:
                node["id"] = unquote(key.removeprefix(node_prefix))
            nodes.append(node)

    edges = []
    for key in kv_store.get_keys_by_prefix(edge_prefix):
        edge = _load_json(key)
        if edge:
            if "id" not in edge:
                edge["id"] = unquote(key.removeprefix(edge_prefix))
            edges.append(edge)

    nodes.sort(key=lambda item: str(item.get("id", "")))
    edges.sort(key=lambda item: str(item.get("id", "")))

    return {
        "session_id": session_id,
        "version": _get_snapshot_version(session_id),
        "nodes": nodes,
        "edges": edges,
    }


def emit_topology_event(
    event_type: str,
    *,
    session_id: Optional[str] = None,
    include_snapshot: bool = False,
    **payload,
) -> dict:
    resolved_session_id = session_id or payload.get("session_id") or _get_current_session_id()
    traceparent = payload.get("traceparent") or _get_current_traceparent()

    event = {
        "type": event_type,
        "timestamp_ms": int(time.time() * 1000),
        "session_id": resolved_session_id,
        "traceparent": traceparent,
        "trace_id": payload.get("trace_id") or _trace_id_from_traceparent(traceparent),
    }
    event.update({k: v for k, v in payload.items() if v is not None})

    if include_snapshot and resolved_session_id:
        event["snapshot"] = get_live_topology_snapshot(resolved_session_id)

    _dispatch_event(event)
    return event


def upsert_topology_node(
    session_id: Optional[str],
    node_id: str,
    *,
    status: str,
    kind: str = "agent",
    **payload,
) -> Optional[dict]:
    if not session_id or not node_id:
        return None

    with _snapshot_lock:
        key = _node_key(session_id, node_id)
        node = _load_json(key) or {"id": node_id, "kind": kind}
        node.update({k: v for k, v in payload.items() if v is not None})
        node["status"] = status
        node["kind"] = kind
        node["updated_at_ms"] = int(time.time() * 1000)
        _store_json(key, node)
        version = _next_snapshot_version(session_id)

    return emit_topology_event(
        f"topology.node.{status}",
        session_id=session_id,
        include_snapshot=True,
        snapshot_version=version,
        node=node,
    )


def upsert_topology_edge(
    session_id: Optional[str],
    source: str,
    target: str,
    *,
    transport: str,
    status: str,
    **payload,
) -> Optional[dict]:
    if not session_id or not source or not target:
        return None

    with _snapshot_lock:
        key = _edge_key(session_id, source, target, transport)
        edge = _load_json(key) or {
            "id": f"{transport}:{source}->{target}",
            "source": source,
            "target": target,
            "transport": transport,
        }
        edge.update({k: v for k, v in payload.items() if v is not None})
        edge["source"] = source
        edge["target"] = target
        edge["transport"] = transport
        edge["status"] = status
        edge["updated_at_ms"] = int(time.time() * 1000)
        _store_json(key, edge)
        version = _next_snapshot_version(session_id)

    return emit_topology_event(
        "topology.edge.updated",
        session_id=session_id,
        include_snapshot=True,
        snapshot_version=version,
        edge=edge,
    )


def record_session_event(session_id: Optional[str], status: str, **payload) -> Optional[dict]:
    if not session_id:
        return None

    with _snapshot_lock:
        version = _next_snapshot_version(session_id)

    return emit_topology_event(
        f"topology.session.{status}",
        session_id=session_id,
        include_snapshot=True,
        snapshot_version=version,
        status=status,
        **payload,
    )

