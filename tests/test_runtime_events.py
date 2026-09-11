# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from opentelemetry import trace
from opentelemetry.semconv_ai import SpanAttributes

from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.decorators import agent, tool
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
from ioa_observe.sdk import Observe
from ioa_observe.sdk.tracing import (
    RuntimeEvent,
    RuntimeEventAttribute,
    RuntimeEventName,
    build_runtime_event_attributes,
    clear_runtime_event_listeners,
    register_runtime_event_listener,
    session_start,
    unregister_runtime_event_listener,
)
from ioa_observe.sdk.config import set_realtime_observability_enabled
from ioa_observe.sdk.tracing.runtime_events import validate_runtime_event_attributes
from ioa_observe.sdk.tracing.topology import clear_topology_listeners


@pytest.fixture(autouse=True)
def reset_runtime_event_state():
    clear_runtime_event_listeners()
    clear_topology_listeners()
    set_realtime_observability_enabled(None)
    with kv_store._lock:
        kv_store.store.clear()
    yield
    clear_runtime_event_listeners()
    clear_topology_listeners()
    set_realtime_observability_enabled(None)
    with kv_store._lock:
        kv_store.store.clear()


@pytest.fixture
def runtime_events():
    events = []
    register_runtime_event_listener(events.append)
    yield events
    unregister_runtime_event_listener(events.append)


@agent(name="runtime_planner", description="Plans tasks")
def runtime_planner(payload: dict) -> dict:
    return {"planned": payload["task"]}


@agent(name="runtime_executor", description="Executes tasks")
def runtime_executor(payload: dict) -> dict:
    return {"result": payload["planned"]}


@tool(name="runtime_tool", description="Runs a tool")
def runtime_tool(payload: dict) -> dict:
    return {"tool_result": payload["task"]}


def test_builds_session_started_runtime_event_attributes():
    attributes = build_runtime_event_attributes(
        RuntimeEventName.TOPOLOGY_SESSION_STARTED,
        session_id="session-123",
        snapshot_version=1,
    )

    assert attributes[RuntimeEventAttribute.EVENT_NAME.value] == (
        RuntimeEventName.TOPOLOGY_SESSION_STARTED.value
    )
    assert attributes[RuntimeEventAttribute.SESSION_ID.value] == "session-123"
    assert attributes[RuntimeEventAttribute.SNAPSHOT_VERSION.value] == 1
    assert RuntimeEventAttribute.EVENT_TIME.value in attributes


def test_builds_a2a_message_runtime_event_attributes():
    attributes = build_runtime_event_attributes(
        RuntimeEventName.A2A_MESSAGE_SENT,
        session_id="session-123",
        snapshot_version=2,
        **{
            RuntimeEventAttribute.SOURCE_AGENT.value: "planner",
            RuntimeEventAttribute.TARGET_AGENT.value: "executor",
            RuntimeEventAttribute.MESSAGE_ID.value: "msg-123",
            RuntimeEventAttribute.FORK_ID.value: "fork-1",
            RuntimeEventAttribute.SEQUENCE.value: 7,
        },
    )

    assert attributes[RuntimeEventAttribute.EVENT_NAME.value] == (
        RuntimeEventName.A2A_MESSAGE_SENT.value
    )
    assert attributes[RuntimeEventAttribute.SOURCE_AGENT.value] == "planner"
    assert attributes[RuntimeEventAttribute.TARGET_AGENT.value] == "executor"
    assert attributes[RuntimeEventAttribute.MESSAGE_ID.value] == "msg-123"
    assert attributes[RuntimeEventAttribute.FORK_ID.value] == "fork-1"
    assert attributes[RuntimeEventAttribute.SEQUENCE.value] == 7


def test_runtime_event_converts_non_otel_values_to_strings():
    event_time = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    deadline = datetime(2026, 6, 10, 12, 1, tzinfo=timezone.utc)
    event = RuntimeEvent(
        name=RuntimeEventName.TOPOLOGY_NODE_STARTED,
        session_id="session-123",
        snapshot_version=3,
        event_time=event_time,
        attributes={
            RuntimeEventAttribute.AGENT_NAME.value: "planner",
            "custom.deadline": deadline,
            "custom.tags": ["live", "topology"],
            "custom.none": None,
        },
    )

    attributes = event.to_otel_attributes()

    assert attributes[RuntimeEventAttribute.EVENT_TIME.value] == event_time.isoformat()
    assert attributes["custom.deadline"] == deadline.isoformat()
    assert attributes["custom.tags"] == "['live', 'topology']"
    assert "custom.none" not in attributes


def test_validation_rejects_missing_required_attributes():
    with pytest.raises(ValueError, match="source.agent"):
        validate_runtime_event_attributes(
            RuntimeEventName.TOPOLOGY_EDGE_UPDATED.value,
            {
                RuntimeEventAttribute.EVENT_NAME.value: (
                    RuntimeEventName.TOPOLOGY_EDGE_UPDATED.value
                ),
                RuntimeEventAttribute.EVENT_TIME.value: (
                    datetime.now(timezone.utc).isoformat()
                ),
                RuntimeEventAttribute.SESSION_ID.value: "session-123",
                RuntimeEventAttribute.SNAPSHOT_VERSION.value: 4,
            },
        )


def test_validation_rejects_unknown_runtime_event_name():
    with pytest.raises(ValueError, match="Unknown runtime event name"):
        validate_runtime_event_attributes(
            "unknown.event",
            {
                RuntimeEventAttribute.EVENT_NAME.value: "unknown.event",
                RuntimeEventAttribute.EVENT_TIME.value: (
                    datetime.now(timezone.utc).isoformat()
                ),
                RuntimeEventAttribute.SESSION_ID.value: "session-123",
                RuntimeEventAttribute.SNAPSHOT_VERSION.value: 1,
            },
        )


def test_runtime_event_logger_bootstraps_without_app_logging_enabled():
    from opentelemetry._logs import get_logger

    Observe.init(
        app_name="runtime-events-test",
        api_endpoint="http://localhost:4318",
        api_key="x",
    )
    with session_start():
        pass

    assert type(get_logger("ioa_observe.runtime_events")).__name__ == "Logger"


def test_session_start_pushes_runtime_event(runtime_events):
    with session_start() as metadata:
        session_id = metadata["executionID"]

    session_event = next(
        event
        for event in runtime_events
        if event[RuntimeEventAttribute.EVENT_NAME.value]
        == RuntimeEventName.TOPOLOGY_SESSION_STARTED.value
    )

    assert session_event[RuntimeEventAttribute.SESSION_ID.value] == session_id
    assert session_event[RuntimeEventAttribute.SNAPSHOT_VERSION.value] >= 1


def test_agent_lifecycle_pushes_runtime_events(runtime_events):
    with session_start():
        plan = runtime_planner({"task": "draft"})
        result = runtime_executor(plan)

    assert result == {"result": "draft"}

    event_names = [
        event[RuntimeEventAttribute.EVENT_NAME.value] for event in runtime_events
    ]
    assert RuntimeEventName.TOPOLOGY_NODE_STARTED.value in event_names
    assert RuntimeEventName.TOPOLOGY_NODE_COMPLETED.value in event_names
    assert RuntimeEventName.TOPOLOGY_EDGE_UPDATED.value in event_names

    agent_names = {
        event.get(RuntimeEventAttribute.AGENT_NAME.value)
        for event in runtime_events
        if event[RuntimeEventAttribute.EVENT_NAME.value]
        in {
            RuntimeEventName.TOPOLOGY_NODE_STARTED.value,
            RuntimeEventName.TOPOLOGY_NODE_COMPLETED.value,
        }
    }
    assert {"runtime_planner", "runtime_executor"}.issubset(agent_names)
    completed_events = {
        event[RuntimeEventAttribute.AGENT_NAME.value]: event
        for event in runtime_events
        if event[RuntimeEventAttribute.EVENT_NAME.value]
        == RuntimeEventName.TOPOLOGY_NODE_COMPLETED.value
    }
    assert json.loads(
        completed_events["runtime_planner"][RuntimeEventAttribute.AGENT_OUTPUT.value]
    ) == {"planned": "draft"}
    assert json.loads(
        completed_events["runtime_executor"][RuntimeEventAttribute.AGENT_OUTPUT.value]
    ) == {"result": "draft"}


def test_a2a_helpers_push_runtime_events(runtime_events):
    request = SimpleNamespace(
        params=SimpleNamespace(
            metadata={
                "observe": {
                    "session_id": "session-123",
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
        event
        for event in runtime_events
        if event[RuntimeEventAttribute.EVENT_NAME.value]
        == RuntimeEventName.A2A_MESSAGE_SENT.value
    )
    received = next(
        event
        for event in runtime_events
        if event[RuntimeEventAttribute.EVENT_NAME.value]
        == RuntimeEventName.A2A_MESSAGE_RECEIVED.value
    )

    assert sent[RuntimeEventAttribute.SOURCE_AGENT.value] == "planner"
    assert sent[RuntimeEventAttribute.TARGET_AGENT.value] == "executor"
    assert sent[RuntimeEventAttribute.MESSAGE_ID.value] == "msg-123"
    assert sent[RuntimeEventAttribute.FORK_ID.value] == "fork-1"
    assert sent[RuntimeEventAttribute.SEQUENCE.value] == 2
    assert received[RuntimeEventAttribute.SOURCE_AGENT.value] == "planner"
    assert received[RuntimeEventAttribute.TARGET_AGENT.value] == "executor"


def test_slim_helpers_push_runtime_events(runtime_events):
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
        event
        for event in runtime_events
        if event[RuntimeEventAttribute.EVENT_NAME.value]
        == RuntimeEventName.SLIM_MESSAGE_SENT.value
    )
    received = next(
        event
        for event in runtime_events
        if event[RuntimeEventAttribute.EVENT_NAME.value]
        == RuntimeEventName.SLIM_MESSAGE_RECEIVED.value
    )

    assert sent[RuntimeEventAttribute.SOURCE_AGENT.value] == "planner"
    assert sent[RuntimeEventAttribute.TARGET_AGENT.value] == "slim://executor"
    assert sent[RuntimeEventAttribute.FORK_ID.value] == "fork-1"
    assert sent[RuntimeEventAttribute.SEQUENCE.value] == 2
    assert sent["network.protocol.name"] == "slim"
    assert received[RuntimeEventAttribute.SOURCE_AGENT.value] == "planner"
    assert received[RuntimeEventAttribute.TARGET_AGENT.value] == "slim://executor"
    assert received["operation.name"] == "get_message_async"


def test_mcp_helpers_push_runtime_events(runtime_events):
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
        event
        for event in runtime_events
        if event[RuntimeEventAttribute.EVENT_NAME.value]
        == RuntimeEventName.MCP_MESSAGE_SENT.value
    )
    received = next(
        event
        for event in runtime_events
        if event[RuntimeEventAttribute.EVENT_NAME.value]
        == RuntimeEventName.MCP_MESSAGE_RECEIVED.value
    )

    assert sent[RuntimeEventAttribute.SOURCE_AGENT.value] == "planner"
    assert sent[RuntimeEventAttribute.TARGET_AGENT.value] == "mcp://math-server"
    assert sent[RuntimeEventAttribute.MESSAGE_ID.value] == "req-1"
    assert sent["network.protocol.name"] == "mcp"
    assert received[RuntimeEventAttribute.SOURCE_AGENT.value] == "planner"
    assert received[RuntimeEventAttribute.TARGET_AGENT.value] == "mcp://math-server"
    assert received[RuntimeEventAttribute.MESSAGE_ID.value] == "req-1"


def test_tool_lifecycle_pushes_runtime_events(runtime_events):
    with session_start():
        result = runtime_tool({"task": "lookup"})

    assert result == {"tool_result": "lookup"}

    event_names = [
        event[RuntimeEventAttribute.EVENT_NAME.value] for event in runtime_events
    ]
    assert RuntimeEventName.TOOL_STARTED.value in event_names
    assert RuntimeEventName.TOOL_COMPLETED.value in event_names

    tool_names = {
        event.get(RuntimeEventAttribute.TOOL_NAME.value)
        for event in runtime_events
        if event[RuntimeEventAttribute.EVENT_NAME.value]
        in {
            RuntimeEventName.TOOL_STARTED.value,
            RuntimeEventName.TOOL_COMPLETED.value,
        }
    }
    assert "runtime_tool" in tool_names
    tool_events = [
        event
        for event in runtime_events
        if event.get(RuntimeEventAttribute.TOOL_NAME.value) == "runtime_tool"
    ]
    started, completed = tool_events
    assert json.loads(started[RuntimeEventAttribute.TOOL_INPUT.value]) == {
        "args": [{"task": "lookup"}],
        "kwargs": {},
    }
    assert json.loads(completed[RuntimeEventAttribute.TOOL_OUTPUT.value]) == {
        "tool_result": "lookup"
    }

    # Tool events must carry a non-zero, monotonically increasing per-session
    # version so downstream materializers can order them deterministically.
    tool_versions = [
        event[RuntimeEventAttribute.SNAPSHOT_VERSION.value]
        for event in runtime_events
        if event[RuntimeEventAttribute.EVENT_NAME.value]
        in {
            RuntimeEventName.TOOL_STARTED.value,
            RuntimeEventName.TOOL_COMPLETED.value,
        }
    ]
    assert all(version > 0 for version in tool_versions)
    assert tool_versions == sorted(tool_versions)
    assert len(set(tool_versions)) == len(tool_versions)


def test_agent_interprets_instrumented_llm_child_spans(runtime_events):
    @agent(name="llm_parent_agent")
    def call_instrumented_llm():
        with trace.get_tracer(__name__).start_as_current_span(
            "openai.chat",
            attributes={
                SpanAttributes.LLM_REQUEST_TYPE: "chat",
                SpanAttributes.LLM_REQUEST_MODEL: "gpt-5",
            },
        ) as span:
            return format(span.get_span_context().span_id, "016x")

    with session_start():
        llm_call_id = call_instrumented_llm()

    llm_events = [
        event
        for event in runtime_events
        if event[RuntimeEventAttribute.EVENT_NAME.value]
        in {
            RuntimeEventName.LLM_STARTED.value,
            RuntimeEventName.LLM_COMPLETED.value,
        }
        and event.get(RuntimeEventAttribute.LLM_CALL_ID.value) == llm_call_id
    ]
    assert {event[RuntimeEventAttribute.EVENT_NAME.value] for event in llm_events} == {
        RuntimeEventName.LLM_STARTED.value,
        RuntimeEventName.LLM_COMPLETED.value,
    }
    assert all(
        event[RuntimeEventAttribute.LLM_NAME.value] == "gpt-5" for event in llm_events
    )
    assert all(
        event[RuntimeEventAttribute.AGENT_NAME.value] == "llm_parent_agent"
        for event in llm_events
    )
    assert all(event["operation.name"] == "chat" for event in llm_events)


def test_agent_interprets_llm_attributes_added_after_span_start(runtime_events):
    @agent(name="llm_parent_agent")
    def call_instrumented_llm():
        with trace.get_tracer(__name__).start_as_current_span(
            "ChatOpenAI.chat"
        ) as span:
            span.set_attribute(SpanAttributes.LLM_SYSTEM, "openai")
            span.set_attribute(SpanAttributes.LLM_REQUEST_MODEL, "gpt-4o")
            span.set_attribute("gen_ai.prompt.0.role", "user")
            span.set_attribute("gen_ai.prompt.0.content", "Hello")
            span.set_attribute("gen_ai.completion.0.role", "assistant")
            span.set_attribute("gen_ai.completion.0.content", "Hi")
            return format(span.get_span_context().span_id, "016x")

    with session_start():
        llm_call_id = call_instrumented_llm()

    llm_events = [
        event
        for event in runtime_events
        if event[RuntimeEventAttribute.EVENT_NAME.value]
        in {
            RuntimeEventName.LLM_STARTED.value,
            RuntimeEventName.LLM_COMPLETED.value,
        }
        and event.get(RuntimeEventAttribute.LLM_CALL_ID.value) == llm_call_id
    ]

    assert [event[RuntimeEventAttribute.EVENT_NAME.value] for event in llm_events] == [
        RuntimeEventName.LLM_STARTED.value,
        RuntimeEventName.LLM_COMPLETED.value,
    ]
    assert all(
        event[RuntimeEventAttribute.LLM_NAME.value] == "gpt-4o" for event in llm_events
    )
    started, completed = llm_events
    assert json.loads(started[RuntimeEventAttribute.LLM_INPUT.value]) == [
        {"content": "Hello", "role": "user"}
    ]
    assert json.loads(completed[RuntimeEventAttribute.LLM_OUTPUT.value]) == [
        {"content": "Hi", "role": "assistant"}
    ]
    assert (
        completed[RuntimeEventAttribute.LLM_INPUT.value]
        == (started[RuntimeEventAttribute.LLM_INPUT.value])
    )


def test_observe_init_can_disable_realtime_runtime_events(runtime_events):
    Observe.init(
        app_name="realtime-disabled-runtime-events",
        exporter=None,
        api_endpoint="http://localhost:4318",
        api_key="x",
        realtime_observability_enabled=False,
    )

    with session_start():
        runtime_tool({"task": "lookup"})

    assert runtime_events == []
