# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from ioa_observe.sdk.tracing.runtime_events import (
    RuntimeEventAttribute,
    RuntimeEventName,
)


@dataclass(frozen=True)
class RuntimeEventRecord:
    name: str
    session_id: str
    snapshot_version: int
    event_time: datetime
    attributes: Mapping[str, Any] = field(default_factory=dict)
    observed_timestamp: str | None = None
    service_name: str | None = None

    @classmethod
    def from_attributes(
        cls,
        attributes: Mapping[str, Any],
        *,
        observed_timestamp: str | None = None,
        service_name: str | None = None,
    ) -> "RuntimeEventRecord":
        event_name = str(attributes[RuntimeEventAttribute.EVENT_NAME.value])
        session_id = str(attributes[RuntimeEventAttribute.SESSION_ID.value])
        snapshot_version = _coerce_int(
            attributes.get(RuntimeEventAttribute.SNAPSHOT_VERSION.value),
            RuntimeEventAttribute.SNAPSHOT_VERSION.value,
        )
        event_time = _parse_datetime(
            attributes.get(RuntimeEventAttribute.EVENT_TIME.value),
            RuntimeEventAttribute.EVENT_TIME.value,
        )
        return cls(
            name=event_name,
            session_id=session_id,
            snapshot_version=snapshot_version,
            event_time=event_time,
            attributes={str(key): value for key, value in attributes.items()},
            observed_timestamp=observed_timestamp,
            service_name=service_name,
        )

    def fingerprint(self) -> tuple[Any, ...]:
        normalized_attributes = tuple(
            sorted(
                (str(key), "" if value is None else str(value))
                for key, value in self.attributes.items()
            )
        )
        return (
            self.name,
            self.session_id,
            self.snapshot_version,
            self.event_time.isoformat(),
            self.observed_timestamp,
            self.service_name,
            normalized_attributes,
        )


@dataclass
class SessionNodeState:
    id: str
    status: str
    version: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    input: str | None = None
    output: str | None = None


@dataclass
class SessionEdgeState:
    id: str
    source: str
    target: str
    kind: str
    transport: str
    status: str
    version: int = 0
    operation: str | None = None
    message_id: str | None = None
    sequence: int | None = None
    fork_id: str | None = None
    updated_at: datetime | None = None


@dataclass
class SessionToolState:
    name: str
    active_count: int = 0
    started_count: int = 0
    completed_count: int = 0
    status: str = "idle"
    version: int = 0
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_input: str | None = None
    last_output: str | None = None


@dataclass
class SessionLLMState:
    name: str
    active_count: int = 0
    started_count: int = 0
    completed_count: int = 0
    status: str = "idle"
    version: int = 0
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_input: str | None = None
    last_output: str | None = None


@dataclass
class SessionState:
    session_id: str
    topology_version: int = 0
    status: str = "idle"
    started_at: datetime | None = None
    last_event_at: datetime | None = None
    last_event_name: str | None = None
    nodes: dict[str, SessionNodeState] = field(default_factory=dict)
    edges: dict[str, SessionEdgeState] = field(default_factory=dict)
    tools: dict[str, SessionToolState] = field(default_factory=dict)
    llms: dict[str, SessionLLMState] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "topology_version": self.topology_version,
            "status": self.status,
            "started_at": _serialize_datetime(self.started_at),
            "last_event_at": _serialize_datetime(self.last_event_at),
            "last_event_name": self.last_event_name,
            "nodes": [
                _serialize_state(node)
                for node in sorted(self.nodes.values(), key=lambda item: item.id)
            ],
            "edges": [
                _serialize_state(edge)
                for edge in sorted(self.edges.values(), key=lambda item: item.id)
            ],
            "tools": [
                _serialize_state(tool)
                for tool in sorted(self.tools.values(), key=lambda item: item.name)
            ],
            "llms": [
                _serialize_state(llm)
                for llm in sorted(self.llms.values(), key=lambda item: item.name)
            ],
        }


class SessionStateMaterializer:
    def __init__(
        self,
        *,
        max_sessions: int | None = 1000,
        session_ttl_seconds: float | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, SessionState] = {}
        # Per-session dedup fingerprints so evicting a session also frees its
        # fingerprint memory (avoids unbounded growth).
        self._event_fingerprints: dict[str, set[tuple[Any, ...]]] = {}
        self._max_sessions = max_sessions
        self._session_ttl_seconds = session_ttl_seconds

    def apply_event(
        self, event: RuntimeEventRecord | Mapping[str, Any]
    ) -> SessionState:
        record = self._to_record(event)
        event_name = RuntimeEventName(record.name)

        with self._lock:
            self._evict_expired()
            session = self._get_or_create_session(record.session_id)
            fingerprint = record.fingerprint()
            fingerprints = self._event_fingerprints.setdefault(record.session_id, set())
            if fingerprint in fingerprints:
                return session

            fingerprints.add(fingerprint)
            self._touch_session(session, record)
            # Enforce the cap after the session has a timestamp so a freshly
            # created session is never selected as the "oldest" and evicted.
            self._enforce_max_sessions(protected_session_id=record.session_id)

            if event_name == RuntimeEventName.TOPOLOGY_SESSION_STARTED:
                self._apply_session_started(session, record)
            elif event_name == RuntimeEventName.TOPOLOGY_SESSION_COMPLETED:
                self._apply_session_completed(session, record)
            elif event_name in {
                RuntimeEventName.TOPOLOGY_NODE_STARTED,
                RuntimeEventName.TOPOLOGY_NODE_COMPLETED,
            }:
                self._apply_node_event(session, record, event_name)
            elif event_name == RuntimeEventName.TOPOLOGY_EDGE_UPDATED:
                self._apply_topology_edge_event(session, record)
            elif event_name in {
                RuntimeEventName.A2A_MESSAGE_SENT,
                RuntimeEventName.A2A_MESSAGE_RECEIVED,
                RuntimeEventName.SLIM_MESSAGE_SENT,
                RuntimeEventName.SLIM_MESSAGE_RECEIVED,
                RuntimeEventName.MCP_MESSAGE_SENT,
                RuntimeEventName.MCP_MESSAGE_RECEIVED,
            }:
                self._apply_protocol_event(session, record, event_name)
            elif event_name in {
                RuntimeEventName.TOOL_STARTED,
                RuntimeEventName.TOOL_COMPLETED,
            }:
                self._apply_tool_event(session, record, event_name)
            elif event_name in {
                RuntimeEventName.LLM_STARTED,
                RuntimeEventName.LLM_COMPLETED,
            }:
                self._apply_llm_event(session, record, event_name)

            return session

    def apply_events(
        self,
        events: Iterable[RuntimeEventRecord | Mapping[str, Any]],
    ) -> list[SessionState]:
        updated_sessions: list[SessionState] = []
        seen_session_ids: set[str] = set()

        for event in events:
            session = self.apply_event(event)
            if session.session_id in seen_session_ids:
                continue
            seen_session_ids.add(session.session_id)
            updated_sessions.append(session)

        return updated_sessions

    def get_session_state(self, session_id: str) -> SessionState | None:
        with self._lock:
            return self._sessions.get(session_id)

    def get_snapshot(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            session = self._sessions.get(session_id)
            return None if session is None else session.snapshot()

    def list_snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._sessions[session_id].snapshot()
                for session_id in sorted(self._sessions.keys())
            ]

    def _apply_session_started(
        self,
        session: SessionState,
        record: RuntimeEventRecord,
    ) -> None:
        session.status = "active"
        session.started_at = session.started_at or record.event_time
        session.topology_version = max(
            session.topology_version, record.snapshot_version
        )

    def _apply_session_completed(
        self,
        session: SessionState,
        record: RuntimeEventRecord,
    ) -> None:
        session.status = "completed"
        session.topology_version = max(
            session.topology_version, record.snapshot_version
        )

    def _apply_node_event(
        self,
        session: SessionState,
        record: RuntimeEventRecord,
        event_name: RuntimeEventName,
    ) -> None:
        agent_name = _required_attribute(
            record,
            RuntimeEventAttribute.AGENT_NAME.value,
        )
        node = session.nodes.get(agent_name)
        if node is None:
            node = SessionNodeState(id=agent_name, status="started")
            session.nodes[agent_name] = node

        if record.snapshot_version < node.version:
            return

        node.version = record.snapshot_version
        session.topology_version = max(
            session.topology_version, record.snapshot_version
        )
        if event_name == RuntimeEventName.TOPOLOGY_NODE_STARTED:
            node.status = "started"
            node.started_at = record.event_time
            node.completed_at = None
            agent_input = _optional_attribute(
                record, RuntimeEventAttribute.AGENT_INPUT.value
            )
            if agent_input is not None:
                node.input = agent_input
        else:
            node.status = "completed"
            node.started_at = node.started_at or record.event_time
            node.completed_at = record.event_time
            agent_output = _optional_attribute(
                record, RuntimeEventAttribute.AGENT_OUTPUT.value
            )
            if agent_output is not None:
                node.output = agent_output

    def _apply_topology_edge_event(
        self,
        session: SessionState,
        record: RuntimeEventRecord,
    ) -> None:
        source = _required_attribute(record, RuntimeEventAttribute.SOURCE_AGENT.value)
        target = _required_attribute(record, RuntimeEventAttribute.TARGET_AGENT.value)
        transport = _optional_attribute(record, "network.protocol.name") or "unknown"
        kind = _optional_attribute(record, "topology.edge.kind") or transport
        status = _optional_attribute(record, "topology.edge.status") or "observed"
        edge_id = _optional_attribute(record, "topology.edge.id") or _edge_id(
            kind,
            transport,
            source,
            target,
        )

        edge = session.edges.get(edge_id)
        if edge is None:
            edge = SessionEdgeState(
                id=edge_id,
                source=source,
                target=target,
                kind=kind,
                transport=transport,
                status=status,
            )
            session.edges[edge_id] = edge

        if record.snapshot_version < edge.version:
            return

        edge.version = record.snapshot_version
        edge.source = source
        edge.target = target
        edge.kind = kind
        edge.transport = transport
        edge.status = status
        edge.operation = _optional_attribute(record, "operation.name")
        edge.message_id = _optional_attribute(
            record, RuntimeEventAttribute.MESSAGE_ID.value
        )
        edge.sequence = _optional_int(record, RuntimeEventAttribute.SEQUENCE.value)
        edge.fork_id = _optional_attribute(record, RuntimeEventAttribute.FORK_ID.value)
        edge.updated_at = record.event_time
        session.topology_version = max(
            session.topology_version, record.snapshot_version
        )

    def _apply_protocol_event(
        self,
        session: SessionState,
        record: RuntimeEventRecord,
        event_name: RuntimeEventName,
    ) -> None:
        source = _required_attribute(record, RuntimeEventAttribute.SOURCE_AGENT.value)
        target = _required_attribute(record, RuntimeEventAttribute.TARGET_AGENT.value)
        transport, kind = _protocol_event_shape(event_name)
        edge_id = f"{transport}:{source}->{target}"
        edge = session.edges.get(edge_id)
        if edge is None:
            edge = SessionEdgeState(
                id=edge_id,
                source=source,
                target=target,
                kind=kind,
                transport=transport,
                status="sent",
            )
            session.edges[edge_id] = edge

        if record.snapshot_version < edge.version:
            return

        edge.version = record.snapshot_version
        edge.source = source
        edge.target = target
        edge.kind = kind
        edge.transport = transport
        edge.status = _protocol_event_status(event_name)
        edge.operation = _optional_attribute(record, "operation.name")
        edge.message_id = _optional_attribute(
            record, RuntimeEventAttribute.MESSAGE_ID.value
        )
        edge.sequence = _optional_int(record, RuntimeEventAttribute.SEQUENCE.value)
        edge.fork_id = _optional_attribute(record, RuntimeEventAttribute.FORK_ID.value)
        edge.updated_at = record.event_time
        session.topology_version = max(
            session.topology_version, record.snapshot_version
        )

    def _apply_tool_event(
        self,
        session: SessionState,
        record: RuntimeEventRecord,
        event_name: RuntimeEventName,
    ) -> None:
        tool_name = _required_attribute(record, RuntimeEventAttribute.TOOL_NAME.value)
        tool = session.tools.get(tool_name)
        if tool is None:
            tool = SessionToolState(name=tool_name)
            session.tools[tool_name] = tool

        # Tool events carry a per-session monotonic version (0 for legacy
        # emitters). Reject stale/out-of-order updates so retries or
        # re-deliveries cannot corrupt the in-flight counters.
        if record.snapshot_version and record.snapshot_version < tool.version:
            return
        tool.version = max(tool.version, record.snapshot_version)

        if event_name == RuntimeEventName.TOOL_STARTED:
            tool.active_count += 1
            tool.started_count += 1
            tool.status = "running"
            tool.last_started_at = record.event_time
            tool_input = _optional_attribute(
                record, RuntimeEventAttribute.TOOL_INPUT.value
            )
            if tool_input is not None:
                tool.last_input = tool_input
        else:
            tool.active_count = max(0, tool.active_count - 1)
            tool.completed_count += 1
            tool.status = "idle" if tool.active_count == 0 else "running"
            tool.last_completed_at = record.event_time
            tool_output = _optional_attribute(
                record, RuntimeEventAttribute.TOOL_OUTPUT.value
            )
            if tool_output is not None:
                tool.last_output = tool_output

        session.status = "active"

    def _apply_llm_event(
        self,
        session: SessionState,
        record: RuntimeEventRecord,
        event_name: RuntimeEventName,
    ) -> None:
        llm_name = _required_attribute(record, RuntimeEventAttribute.LLM_NAME.value)
        llm = session.llms.get(llm_name)
        if llm is None:
            llm = SessionLLMState(name=llm_name)
            session.llms[llm_name] = llm

        if record.snapshot_version and record.snapshot_version < llm.version:
            return
        llm.version = max(llm.version, record.snapshot_version)

        if event_name == RuntimeEventName.LLM_STARTED:
            llm.active_count += 1
            llm.started_count += 1
            llm.status = "running"
            llm.last_started_at = record.event_time
            llm_input = _optional_attribute(
                record, RuntimeEventAttribute.LLM_INPUT.value
            )
            if llm_input is not None:
                llm.last_input = llm_input
        else:
            llm.active_count = max(0, llm.active_count - 1)
            llm.completed_count += 1
            llm.status = "idle" if llm.active_count == 0 else "running"
            llm.last_completed_at = record.event_time
            llm_input = _optional_attribute(
                record, RuntimeEventAttribute.LLM_INPUT.value
            )
            if llm_input is not None:
                llm.last_input = llm_input
            llm_output = _optional_attribute(
                record, RuntimeEventAttribute.LLM_OUTPUT.value
            )
            if llm_output is not None:
                llm.last_output = llm_output

        session.status = "active"

    def _touch_session(self, session: SessionState, record: RuntimeEventRecord) -> None:
        session.status = "active"
        if session.started_at is None:
            session.started_at = record.event_time
        if session.last_event_at is None or record.event_time >= session.last_event_at:
            session.last_event_at = record.event_time
            session.last_event_name = record.name

    def _get_or_create_session(self, session_id: str) -> SessionState:
        session = self._sessions.get(session_id)
        if session is None:
            session = SessionState(session_id=session_id)
            self._sessions[session_id] = session
        return session

    def _evict_expired(self) -> None:
        if not self._session_ttl_seconds:
            return
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - self._session_ttl_seconds
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if session.last_event_at is not None
            and session.last_event_at.timestamp() < cutoff
        ]
        for session_id in expired:
            self._drop_session(session_id)

    def _enforce_max_sessions(self, protected_session_id: str | None = None) -> None:
        if not self._max_sessions:
            return
        while len(self._sessions) > self._max_sessions:
            oldest_id = min(
                self._sessions,
                key=lambda sid: self._session_sort_key(sid),
            )
            if oldest_id == protected_session_id and len(self._sessions) == 1:
                break
            self._drop_session(oldest_id)

    def _session_sort_key(self, session_id: str) -> float:
        session = self._sessions[session_id]
        timestamp = session.last_event_at or session.started_at
        return timestamp.timestamp() if timestamp is not None else 0.0

    def _drop_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._event_fingerprints.pop(session_id, None)

    @staticmethod
    def _to_record(
        event: RuntimeEventRecord | Mapping[str, Any],
    ) -> RuntimeEventRecord:
        if isinstance(event, RuntimeEventRecord):
            return event
        return RuntimeEventRecord.from_attributes(event)


def _required_attribute(record: RuntimeEventRecord, key: str) -> str:
    value = record.attributes.get(key)
    if value is None or value == "":
        raise ValueError(
            f"Runtime event {record.name} missing required attribute: {key}"
        )
    return str(value)


def _optional_attribute(record: RuntimeEventRecord, key: str) -> str | None:
    value = record.attributes.get(key)
    if value is None or value == "":
        return None
    return str(value)


def _optional_int(record: RuntimeEventRecord, key: str) -> int | None:
    value = record.attributes.get(key)
    if value in (None, ""):
        return None
    return _coerce_int(value, key)


def _coerce_int(value: Any, key: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Runtime event attribute {key} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Runtime event attribute {key} must be an integer") from exc


def _parse_datetime(value: Any, key: str) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        raise ValueError(f"Runtime event attribute {key} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"Runtime event attribute {key} must be an ISO timestamp"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _edge_id(kind: str, transport: str, source: str, target: str) -> str:
    if kind == "agent_handoff":
        return f"agent_handoff:{source}->{target}"
    return f"{transport}:{source}->{target}"


def _protocol_event_shape(event_name: RuntimeEventName) -> tuple[str, str]:
    if event_name in {
        RuntimeEventName.A2A_MESSAGE_SENT,
        RuntimeEventName.A2A_MESSAGE_RECEIVED,
    }:
        return ("a2a", "a2a_message")
    if event_name in {
        RuntimeEventName.SLIM_MESSAGE_SENT,
        RuntimeEventName.SLIM_MESSAGE_RECEIVED,
    }:
        return ("slim", "slim_message")
    if event_name in {
        RuntimeEventName.MCP_MESSAGE_SENT,
        RuntimeEventName.MCP_MESSAGE_RECEIVED,
    }:
        return ("mcp", "mcp_message")
    raise ValueError(f"Unsupported protocol runtime event: {event_name}")


def _protocol_event_status(event_name: RuntimeEventName) -> str:
    if event_name in {
        RuntimeEventName.A2A_MESSAGE_SENT,
        RuntimeEventName.SLIM_MESSAGE_SENT,
        RuntimeEventName.MCP_MESSAGE_SENT,
    }:
        return "sent"
    if event_name in {
        RuntimeEventName.A2A_MESSAGE_RECEIVED,
        RuntimeEventName.SLIM_MESSAGE_RECEIVED,
        RuntimeEventName.MCP_MESSAGE_RECEIVED,
    }:
        return "received"
    raise ValueError(f"Unsupported protocol runtime event: {event_name}")


def _serialize_state(value: Any) -> dict[str, Any]:
    serialized: dict[str, Any] = {}
    for key, item in asdict(value).items():
        if isinstance(item, datetime):
            serialized[key] = _serialize_datetime(item)
        else:
            serialized[key] = item
    return serialized


def _serialize_datetime(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
