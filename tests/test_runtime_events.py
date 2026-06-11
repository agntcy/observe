# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.decorators import agent
from ioa_observe.sdk.instrumentations.a2a import (
	_emit_a2a_receive_topology_event,
	_emit_a2a_send_topology_event,
)
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
from ioa_observe.sdk.tracing.runtime_events import validate_runtime_event_attributes
from ioa_observe.sdk.tracing.topology import clear_topology_listeners


@pytest.fixture(autouse=True)
def reset_runtime_event_state():
	clear_runtime_event_listeners()
	clear_topology_listeners()
	with kv_store._lock:
		kv_store.store.clear()
	yield
	clear_runtime_event_listeners()
	clear_topology_listeners()
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
