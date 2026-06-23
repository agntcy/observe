# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import pytest

from ioa_observe.sdk import Observe
from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.config import set_realtime_observability_enabled
from ioa_observe.sdk.decorators import agent
from ioa_observe.sdk.instrumentations.a2a import (
    _emit_a2a_receive_topology_event,
    _emit_a2a_send_topology_event,
)
from ioa_observe.sdk.instrumentations.mcp import (
    _emit_mcp_receive_topology_event,
    _emit_mcp_send_topology_event,
)
from ioa_observe.sdk.instrumentations.slim import (
    _emit_slim_receive_topology_event,
    _emit_slim_send_topology_event,
)
from ioa_observe.sdk.tracing import get_live_topology_snapshot, session_start
from ioa_observe.sdk.tracing.topology import clear_topology_listeners


@pytest.fixture(autouse=True)
def reset_topology_state():
    clear_topology_listeners()
    set_realtime_observability_enabled(None)
    with kv_store._lock:
        kv_store.store.clear()
    yield
    clear_topology_listeners()
    set_realtime_observability_enabled(None)
    with kv_store._lock:
        kv_store.store.clear()


@pytest.fixture
def topology_events():
    events = []
    listener = events.append
    Observe.register_topology_listener(listener)
    yield events
    Observe.unregister_topology_listener(listener)


@agent(name="planner", description="Plans tasks")
def planner(payload: dict) -> dict:
    return {"planned": payload["task"]}


@agent(name="executor", description="Executes tasks")
def executor(payload: dict) -> dict:
    return {"result": payload["planned"]}


def test_session_start_emits_live_topology_session_event(topology_events):
    with session_start() as metadata:
        assert metadata["executionID"].startswith("test_")

    assert topology_events
    session_started = next(
        event
        for event in topology_events
        if event["type"] == "topology.session.started"
    )
    assert session_started["session_id"] == metadata["executionID"]
    assert session_started["snapshot_version"] >= 1
    assert session_started["snapshot"]["session_id"] == metadata["executionID"]


def test_agent_events_build_runtime_snapshot(topology_events):
    with session_start() as metadata:
        plan = planner({"task": "draft"})
        result = executor(plan)

    assert result == {"result": "draft"}

    snapshot = get_live_topology_snapshot(metadata["executionID"])
    node_ids = {node["id"]: node for node in snapshot["nodes"]}
    edge_ids = {edge["id"]: edge for edge in snapshot["edges"]}

    assert node_ids["planner"]["status"] == "completed"
    assert node_ids["executor"]["status"] == "completed"
    assert edge_ids["agent_handoff:planner->executor"]["status"] == "observed"

    event_types = [event["type"] for event in topology_events]
    assert "topology.node.started" in event_types
    assert "topology.node.completed" in event_types
    assert "topology.edge.updated" in event_types


def test_a2a_send_and_receive_emit_live_edge_events(topology_events):
    request = SimpleNamespace(
        params=SimpleNamespace(
            metadata={
                "observe": {
                    "session_id": "session-123",
                    "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
                    "last_agent_name": "planner",
                    "agent_sequence": "2",
                    "fork_id": "fork-1",
                }
            },
            message=SimpleNamespace(messageId="msg-123"),
        )
    )
    client = SimpleNamespace(agent_card=SimpleNamespace(name="executor"))
    handler = SimpleNamespace(name="executor")

    _emit_a2a_send_topology_event(request, client, "send_message")
    _emit_a2a_receive_topology_event(request.params, handler, "on_message_send")

    sent = next(
        event for event in topology_events if event["type"] == "a2a.message.sent"
    )
    received = next(
        event for event in topology_events if event["type"] == "a2a.message.received"
    )

    assert sent["source"] == "planner"
    assert sent["target"] == "executor"
    assert sent["message_id"] == "msg-123"
    assert sent["fork_id"] == "fork-1"

    assert received["source"] == "planner"
    assert received["target"] == "executor"
    assert received["message_id"] == "msg-123"

    snapshot = get_live_topology_snapshot("session-123")
    assert len(snapshot["edges"]) == 1
    edge = snapshot["edges"][0]
    assert edge["id"] == "a2a:planner->executor"
    assert edge["kind"] == "a2a_message"
    assert edge["fork_id"] == "fork-1"
    assert edge["message_id"] == "msg-123"
    assert edge["operation"] == "on_message_send"
    assert edge["sequence"] == 2
    assert edge["source"] == "planner"
    assert edge["status"] == "received"
    assert edge["target"] == "executor"
    assert edge["transport"] == "a2a"
    assert edge["updated_at_ms"] > 0
    assert snapshot["version"] >= 2


def test_slim_send_and_receive_emit_live_edge_events(topology_events):
    headers = {
        "session_id": "session-123",
        "source_agent": "planner",
        "target_agent": "slim://executor",
        "agent_sequence": "2",
        "fork_id": "fork-1",
    }

    _emit_slim_send_topology_event(headers, "publish_to_async")
    _emit_slim_receive_topology_event(headers, "get_message_async")

    sent = next(
        event for event in topology_events if event["type"] == "slim.message.sent"
    )
    received = next(
        event for event in topology_events if event["type"] == "slim.message.received"
    )

    assert sent["source"] == "planner"
    assert sent["target"] == "slim://executor"
    assert sent["fork_id"] == "fork-1"
    assert received["source"] == "planner"
    assert received["target"] == "slim://executor"

    snapshot = get_live_topology_snapshot("session-123")
    assert len(snapshot["edges"]) == 1
    edge = snapshot["edges"][0]
    assert edge["id"] == "slim:planner->slim://executor"
    assert edge["kind"] == "slim_message"
    assert edge["fork_id"] == "fork-1"
    assert edge["operation"] == "get_message_async"
    assert edge["sequence"] == 2
    assert edge["source"] == "planner"
    assert edge["status"] == "received"
    assert edge["target"] == "slim://executor"
    assert edge["transport"] == "slim"
    assert edge["updated_at_ms"] > 0
    assert snapshot["version"] >= 2


def test_mcp_send_and_receive_emit_live_edge_events(topology_events):
    observe_meta = {
        "session.id": "session-123",
        "source_agent": "planner",
        "target_agent": "mcp://math-server",
        "agent_sequence": "2",
        "fork_id": "fork-1",
    }

    _emit_mcp_send_topology_event(observe_meta, "tools/call", message_id="req-1")
    _emit_mcp_receive_topology_event(
        observe_meta,
        "tools/call",
        message_id="req-1",
    )

    sent = next(
        event for event in topology_events if event["type"] == "mcp.message.sent"
    )
    received = next(
        event for event in topology_events if event["type"] == "mcp.message.received"
    )

    assert sent["source"] == "planner"
    assert sent["target"] == "mcp://math-server"
    assert sent["message_id"] == "req-1"
    assert received["source"] == "planner"
    assert received["target"] == "mcp://math-server"
    assert received["message_id"] == "req-1"

    snapshot = get_live_topology_snapshot("session-123")
    assert len(snapshot["edges"]) == 1
    edge = snapshot["edges"][0]
    assert edge["id"] == "mcp:planner->mcp://math-server"
    assert edge["kind"] == "mcp_message"
    assert edge["fork_id"] == "fork-1"
    assert edge["message_id"] == "req-1"
    assert edge["operation"] == "tools/call"
    assert edge["sequence"] == 2
    assert edge["source"] == "planner"
    assert edge["status"] == "received"
    assert edge["target"] == "mcp://math-server"
    assert edge["transport"] == "mcp"
    assert edge["updated_at_ms"] > 0
    assert snapshot["version"] >= 2


def test_observe_init_can_disable_realtime_topology_events(topology_events):
    Observe.init(
        app_name="realtime-disabled-topology",
        exporter=None,
        api_endpoint="http://localhost:4318",
        api_key="x",
        realtime_observability_enabled=False,
    )

    with session_start() as metadata:
        plan = planner({"task": "draft"})
        result = executor(plan)

    assert result == {"result": "draft"}
    assert topology_events == []

    snapshot = get_live_topology_snapshot(metadata["executionID"])
    node_ids = {node["id"]: node for node in snapshot["nodes"]}
    edge_ids = {edge["id"]: edge for edge in snapshot["edges"]}
    assert node_ids["planner"]["status"] == "completed"
    assert node_ids["executor"]["status"] == "completed"
    assert edge_ids["agent_handoff:planner->executor"]["status"] == "observed"
