# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from ioa_observe.sdk.tracing.runtime_event_emitter import emit_runtime_event
from ioa_observe.sdk.tracing.runtime_events import (
	RuntimeEventAttribute,
	RuntimeEventName,
	build_runtime_event_attributes,
)


TopologyListener = Callable[[dict[str, Any]], None]

_listeners: list[TopologyListener] = []
_session_graphs: dict[str, "SessionGraph"] = {}
_lock = threading.RLock()


@dataclass
class TopologyNode:
	id: str
	status: str
	started_at_ms: int
	completed_at_ms: int | None = None


@dataclass
class TopologyEdge:
	id: str
	source: str
	target: str
	kind: str
	transport: str
	status: str
	operation: str | None = None
	message_id: str | None = None
	sequence: int | None = None
	fork_id: str | None = None
	updated_at_ms: int = 0


@dataclass
class SessionGraph:
	session_id: str
	version: int = 0
	nodes: dict[str, TopologyNode] = field(default_factory=dict)
	edges: dict[str, TopologyEdge] = field(default_factory=dict)

	def snapshot(self) -> dict[str, Any]:
		return {
			"session_id": self.session_id,
			"version": self.version,
			"nodes": [asdict(node) for node in self.nodes.values()],
			"edges": [asdict(edge) for edge in self.edges.values()],
		}


def register_topology_listener(listener: TopologyListener) -> None:
	with _lock:
		if listener not in _listeners:
			_listeners.append(listener)


def unregister_topology_listener(listener: TopologyListener) -> None:
	with _lock:
		if listener in _listeners:
			_listeners.remove(listener)


def clear_topology_listeners() -> None:
	with _lock:
		_listeners.clear()
		_session_graphs.clear()


def record_session_started(session_id: str) -> None:
	with _lock:
		graph = _get_or_create_graph(session_id)
		graph.version += 1
		snapshot = graph.snapshot()
		event = {
			"type": RuntimeEventName.TOPOLOGY_SESSION_STARTED.value,
			"session_id": session_id,
			"snapshot_version": graph.version,
			"snapshot": snapshot,
		}
		runtime_attributes = build_runtime_event_attributes(
			RuntimeEventName.TOPOLOGY_SESSION_STARTED,
			session_id=session_id,
			snapshot_version=graph.version,
		)

	_publish(event, runtime_attributes)


def record_node_started(session_id: str, agent_name: str) -> None:
	now_ms = _now_ms()
	with _lock:
		graph = _get_or_create_graph(session_id)
		node = graph.nodes.get(agent_name)
		if node is None:
			node = TopologyNode(
				id=agent_name,
				status="started",
				started_at_ms=now_ms,
			)
			graph.nodes[agent_name] = node
		else:
			node.status = "started"
			node.completed_at_ms = None

		graph.version += 1
		event = {
			"type": RuntimeEventName.TOPOLOGY_NODE_STARTED.value,
			"session_id": session_id,
			"agent_name": agent_name,
			"snapshot_version": graph.version,
			"snapshot": graph.snapshot(),
		}
		runtime_attributes = build_runtime_event_attributes(
			RuntimeEventName.TOPOLOGY_NODE_STARTED,
			session_id=session_id,
			snapshot_version=graph.version,
			**{RuntimeEventAttribute.AGENT_NAME.value: agent_name},
		)

	_publish(event, runtime_attributes)


def record_node_completed(session_id: str, agent_name: str) -> None:
	now_ms = _now_ms()
	with _lock:
		graph = _get_or_create_graph(session_id)
		node = graph.nodes.get(agent_name)
		if node is None:
			node = TopologyNode(
				id=agent_name,
				status="completed",
				started_at_ms=now_ms,
				completed_at_ms=now_ms,
			)
			graph.nodes[agent_name] = node
		else:
			node.status = "completed"
			node.completed_at_ms = now_ms

		graph.version += 1
		event = {
			"type": RuntimeEventName.TOPOLOGY_NODE_COMPLETED.value,
			"session_id": session_id,
			"agent_name": agent_name,
			"snapshot_version": graph.version,
			"snapshot": graph.snapshot(),
		}
		runtime_attributes = build_runtime_event_attributes(
			RuntimeEventName.TOPOLOGY_NODE_COMPLETED,
			session_id=session_id,
			snapshot_version=graph.version,
			**{RuntimeEventAttribute.AGENT_NAME.value: agent_name},
		)

	_publish(event, runtime_attributes)


def upsert_topology_edge(
	session_id: str,
	source: str,
	target: str,
	*,
	transport: str,
	status: str,
	operation: str | None = None,
	message_id: str | None = None,
	sequence: int | None = None,
	fork_id: str | None = None,
	kind: str = "agent_handoff",
) -> None:
	edge_id = f"{transport}:{source}->{target}"
	if kind == "agent_handoff":
		edge_id = f"agent_handoff:{source}->{target}"
	now_ms = _now_ms()

	with _lock:
		graph = _get_or_create_graph(session_id)
		graph.edges[edge_id] = TopologyEdge(
			id=edge_id,
			source=source,
			target=target,
			kind=kind,
			transport=transport,
			status=status,
			operation=operation,
			message_id=message_id,
			sequence=sequence,
			fork_id=fork_id,
			updated_at_ms=now_ms,
		)
		graph.version += 1
		event = {
			"type": RuntimeEventName.TOPOLOGY_EDGE_UPDATED.value,
			"session_id": session_id,
			"edge_id": edge_id,
			"snapshot_version": graph.version,
			"snapshot": graph.snapshot(),
		}
		runtime_attributes = build_runtime_event_attributes(
			RuntimeEventName.TOPOLOGY_EDGE_UPDATED,
			session_id=session_id,
			snapshot_version=graph.version,
			**{
				RuntimeEventAttribute.SOURCE_AGENT.value: source,
				RuntimeEventAttribute.TARGET_AGENT.value: target,
				RuntimeEventAttribute.MESSAGE_ID.value: message_id,
				RuntimeEventAttribute.FORK_ID.value: fork_id,
				RuntimeEventAttribute.SEQUENCE.value: sequence,
				"topology.edge.id": edge_id,
				"topology.edge.kind": kind,
				"topology.edge.status": status,
				"network.protocol.name": transport,
				"operation.name": operation,
			},
		)

	_publish(event, runtime_attributes)


def emit_topology_event(
	event_type: str,
	*,
	session_id: str,
	include_snapshot: bool = False,
	**attributes: Any,
) -> None:
	with _lock:
		graph = _get_or_create_graph(session_id)
		event = {
			"type": event_type,
			"session_id": session_id,
			"snapshot_version": graph.version,
			**attributes,
		}
		if include_snapshot:
			event["snapshot"] = graph.snapshot()

		runtime_attributes = _runtime_attributes_for_event(
			event_type,
			session_id,
			graph.version,
			attributes,
		)

	_publish(event, runtime_attributes)


def get_live_topology_snapshot(session_id: str) -> dict[str, Any]:
	with _lock:
		graph = _session_graphs.get(session_id)
		if graph is None:
			return {"session_id": session_id, "version": 0, "nodes": [], "edges": []}
		return graph.snapshot()


def _runtime_attributes_for_event(
	event_type: str,
	session_id: str,
	snapshot_version: int,
	attributes: dict[str, Any],
) -> dict[str, str | bool | int | float]:
	event_name = RuntimeEventName(event_type)
	payload: dict[str, Any] = {}
	if "agent_name" in attributes:
		payload[RuntimeEventAttribute.AGENT_NAME.value] = attributes["agent_name"]
	if "source" in attributes:
		payload[RuntimeEventAttribute.SOURCE_AGENT.value] = attributes["source"]
	if "target" in attributes:
		payload[RuntimeEventAttribute.TARGET_AGENT.value] = attributes["target"]
	if "message_id" in attributes:
		payload[RuntimeEventAttribute.MESSAGE_ID.value] = attributes["message_id"]
	if "fork_id" in attributes:
		payload[RuntimeEventAttribute.FORK_ID.value] = attributes["fork_id"]
	if "sequence" in attributes:
		payload[RuntimeEventAttribute.SEQUENCE.value] = attributes["sequence"]
	if "protocol" in attributes:
		payload["network.protocol.name"] = attributes["protocol"]
	if "operation" in attributes:
		payload["operation.name"] = attributes["operation"]

	return build_runtime_event_attributes(
		event_name,
		session_id=session_id,
		snapshot_version=snapshot_version,
		**payload,
	)


def _publish(
	event: dict[str, Any],
	runtime_attributes: dict[str, str | bool | int | float],
) -> None:
	emit_runtime_event(runtime_attributes)
	_notify_listeners(event)


def _notify_listeners(event: dict[str, Any]) -> None:
	with _lock:
		listeners = tuple(_listeners)

	for listener in listeners:
		listener(dict(event))


def _get_or_create_graph(session_id: str) -> SessionGraph:
	graph = _session_graphs.get(session_id)
	if graph is None:
		graph = SessionGraph(session_id=session_id)
		_session_graphs[session_id] = graph
	return graph


def _now_ms() -> int:
	return int(time.time() * 1000)
