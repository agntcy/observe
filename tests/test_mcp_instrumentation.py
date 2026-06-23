# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from opentelemetry.trace import get_tracer

from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.instrumentations.mcp import McpInstrumentor
from ioa_observe.sdk.tracing import (
    RuntimeEventAttribute,
    RuntimeEventName,
    clear_runtime_event_listeners,
    get_live_topology_snapshot,
    register_runtime_event_listener,
    unregister_runtime_event_listener,
)
from ioa_observe.sdk.tracing.topology import clear_topology_listeners


@pytest.fixture(autouse=True)
def reset_mcp_realtime_state():
    clear_runtime_event_listeners()
    clear_topology_listeners()
    with kv_store._lock:
        kv_store.store.clear()
    yield
    clear_runtime_event_listeners()
    clear_topology_listeners()
    with kv_store._lock:
        kv_store.store.clear()


def test_patch_mcp_client_injects_realtime_protocol_metadata_and_events():
    runtime_events = []
    register_runtime_event_listener(runtime_events.append)

    class Params:
        def __init__(self):
            self.meta = None

    class Root:
        def __init__(self):
            self.method = "tools/call"
            self.params = Params()
            self.id = "req-1"

    request = SimpleNamespace(root=Root())
    session = SimpleNamespace(
        _write_stream=SimpleNamespace(observe_peer_name="mcp://math-server")
    )

    traceparent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    session_id = "session-123"
    kv_store.set(f"execution.{traceparent}", session_id)
    kv_store.set(f"session.{session_id}.last_agent_name", "planner")
    kv_store.set(f"session.{session_id}.agent_sequence", "2")
    kv_store.set(f"session.{session_id}.agents.2.fork_id", "fork-1")

    def context_value(key):
        return {"session.id": session_id, "workflow_name": "planner"}.get(key)

    async def wrapped(*args, **kwargs):
        return SimpleNamespace(isError=False, content=[])

    instrumented = McpInstrumentor().patch_mcp_client(get_tracer(__name__))

    try:
        with patch(
            "ioa_observe.sdk.instrumentations.mcp.get_value",
            side_effect=context_value,
        ), patch(
            "ioa_observe.sdk.tracing.get_current_traceparent",
            return_value=traceparent,
        ):
            result = asyncio.run(instrumented(wrapped, session, (request,), {}))
    finally:
        unregister_runtime_event_listener(runtime_events.append)

    assert result.isError is False
    assert request.root.params.meta["session.id"] == session_id
    assert request.root.params.meta["traceparent"] == traceparent
    assert request.root.params.meta["source_agent"] == "planner"
    assert request.root.params.meta["target_agent"] == "mcp://math-server"

    sent = next(
        event
        for event in runtime_events
        if event[RuntimeEventAttribute.EVENT_NAME.value]
        == RuntimeEventName.MCP_MESSAGE_SENT.value
    )
    assert sent[RuntimeEventAttribute.SOURCE_AGENT.value] == "planner"
    assert sent[RuntimeEventAttribute.TARGET_AGENT.value] == "mcp://math-server"
    assert sent[RuntimeEventAttribute.MESSAGE_ID.value] == "req-1"
    assert sent[RuntimeEventAttribute.SEQUENCE.value] == 2
    assert sent[RuntimeEventAttribute.FORK_ID.value] == "fork-1"

    snapshot = get_live_topology_snapshot(session_id)
    assert snapshot["edges"][0]["id"] == "mcp:planner->mcp://math-server"
    assert snapshot["edges"][0]["status"] == "sent"



