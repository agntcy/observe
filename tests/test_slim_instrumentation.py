# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import types
from unittest.mock import patch

import pytest

from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.instrumentations.slim import SLIMInstrumentor
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
def reset_slim_realtime_state():
    clear_runtime_event_listeners()
    clear_topology_listeners()
    with kv_store._lock:
        kv_store.store.clear()
    yield
    clear_runtime_event_listeners()
    clear_topology_listeners()
    with kv_store._lock:
        kv_store.store.clear()


def test_create_session_async_accepts_destination_kwarg():
    captured = {}

    class App:
        async def create_session_async(self, config, destination=None, *args, **kwargs):
            captured["destination"] = destination

    fake_bindings = types.SimpleNamespace(App=App)

    with patch("ioa_observe.sdk.instrumentations.slim.TracerWrapper") as tw:
        tw.return_value.get_tracer.return_value = None
        SLIMInstrumentor()._instrument_app(fake_bindings)

    asyncio.run(App.create_session_async(App(), "config", destination="dest_name"))
    assert captured["destination"] == "dest_name"


def test_create_session_and_wait_async_accepts_destination_kwarg():
    captured = {}

    class App:
        async def create_session_and_wait_async(
            self, config, destination=None, *args, **kwargs
        ):
            captured["destination"] = destination

    fake_bindings = types.SimpleNamespace(App=App)

    with patch("ioa_observe.sdk.instrumentations.slim.TracerWrapper") as tw:
        tw.return_value.get_tracer.return_value = None
        SLIMInstrumentor()._instrument_app(fake_bindings)

    asyncio.run(
        App.create_session_and_wait_async(App(), "config", destination="dest_name")
    )
    assert captured["destination"] == "dest_name"


def test_publish_to_async_injects_realtime_protocol_metadata_and_events():
    captured = {}
    runtime_events = []
    register_runtime_event_listener(runtime_events.append)

    class Session:
        async def publish_to_async(self, destination, payload, *args, **kwargs):
            captured["destination"] = destination
            captured["payload"] = payload
            return "ok"

    traceparent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    session_id = "session-123"
    kv_store.set(f"execution.{traceparent}", session_id)
    kv_store.set(f"session.{session_id}.last_agent_name", "planner")
    kv_store.set(f"session.{session_id}.agent_sequence", "2")
    kv_store.set(f"session.{session_id}.agents.2.fork_id", "fork-1")

    def context_value(key):
        return {"session.id": session_id, "workflow_name": "planner"}.get(key)

    SLIMInstrumentor()._wrap_publish(Session, "publish_to_async", msg_idx=1)

    try:
        with (
            patch(
                "ioa_observe.sdk.instrumentations.slim.get_current_traceparent",
                return_value=traceparent,
            ),
            patch(
                "ioa_observe.sdk.instrumentations.slim.get_value",
                side_effect=context_value,
            ),
        ):
            result = asyncio.run(
                Session.publish_to_async(
                    Session(),
                    "slim://executor",
                    b'{"task": "draft"}',
                )
            )
    finally:
        unregister_runtime_event_listener(runtime_events.append)

    assert result == "ok"
    wrapped = json.loads(captured["payload"].decode("utf-8"))
    assert wrapped["headers"]["session_id"] == session_id
    assert wrapped["headers"]["traceparent"] == traceparent
    assert wrapped["headers"]["source_agent"] == "planner"
    assert wrapped["headers"]["target_agent"] == "slim://executor"

    sent = next(
        event
        for event in runtime_events
        if event[RuntimeEventAttribute.EVENT_NAME.value]
        == RuntimeEventName.SLIM_MESSAGE_SENT.value
    )
    assert sent[RuntimeEventAttribute.SOURCE_AGENT.value] == "planner"
    assert sent[RuntimeEventAttribute.TARGET_AGENT.value] == "slim://executor"
    assert sent[RuntimeEventAttribute.FORK_ID.value] == "fork-1"
    assert sent[RuntimeEventAttribute.SEQUENCE.value] == 2

    snapshot = get_live_topology_snapshot(session_id)
    assert snapshot["edges"][0]["id"] == "slim:planner->slim://executor"
    assert snapshot["edges"][0]["status"] == "sent"
