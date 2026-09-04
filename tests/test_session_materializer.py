# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ioa_observe.materializer import (
    ClickHouseRuntimeEventConsumer,
    ClickHouseRuntimeEventSource,
    RuntimeEventRecord,
    SessionStateMaterializer,
)
from ioa_observe.sdk.tracing import (
    RuntimeEventAttribute,
    RuntimeEventName,
    build_runtime_event_attributes,
)


def test_materializer_builds_live_session_state_from_runtime_events():
    materializer = SessionStateMaterializer()
    base_time = datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc)

    events = [
        _event(
            RuntimeEventName.TOPOLOGY_SESSION_STARTED,
            base_time,
            session_id="session-123",
            snapshot_version=1,
        ),
        _event(
            RuntimeEventName.TOPOLOGY_NODE_STARTED,
            base_time + timedelta(seconds=1),
            session_id="session-123",
            snapshot_version=2,
            **{RuntimeEventAttribute.AGENT_NAME.value: "planner"},
        ),
        _event(
            RuntimeEventName.TOOL_STARTED,
            base_time + timedelta(seconds=2),
            session_id="session-123",
            snapshot_version=0,
            **{RuntimeEventAttribute.TOOL_NAME.value: "search"},
        ),
        _event(
            RuntimeEventName.TOPOLOGY_EDGE_UPDATED,
            base_time + timedelta(seconds=3),
            session_id="session-123",
            snapshot_version=3,
            **{
                RuntimeEventAttribute.SOURCE_AGENT.value: "planner",
                RuntimeEventAttribute.TARGET_AGENT.value: "executor",
                "topology.edge.id": "agent_handoff:planner->executor",
                "topology.edge.kind": "agent_handoff",
                "topology.edge.status": "observed",
                "network.protocol.name": "agent_handoff",
                "operation.name": "agent_handoff",
                RuntimeEventAttribute.SEQUENCE.value: 2,
            },
        ),
        _event(
            RuntimeEventName.A2A_MESSAGE_RECEIVED,
            base_time + timedelta(seconds=4),
            session_id="session-123",
            snapshot_version=4,
            **{
                RuntimeEventAttribute.SOURCE_AGENT.value: "planner",
                RuntimeEventAttribute.TARGET_AGENT.value: "executor",
                RuntimeEventAttribute.MESSAGE_ID.value: "msg-1",
                RuntimeEventAttribute.FORK_ID.value: "fork-1",
                RuntimeEventAttribute.SEQUENCE.value: 2,
                "operation.name": "on_message_send",
            },
        ),
        _event(
            RuntimeEventName.TOPOLOGY_NODE_COMPLETED,
            base_time + timedelta(seconds=5),
            session_id="session-123",
            snapshot_version=5,
            **{RuntimeEventAttribute.AGENT_NAME.value: "planner"},
        ),
        _event(
            RuntimeEventName.TOOL_COMPLETED,
            base_time + timedelta(seconds=6),
            session_id="session-123",
            snapshot_version=0,
            **{RuntimeEventAttribute.TOOL_NAME.value: "search"},
        ),
        _event(
            RuntimeEventName.LLM_STARTED,
            base_time + timedelta(seconds=7),
            session_id="session-123",
            snapshot_version=6,
            **{
                RuntimeEventAttribute.LLM_NAME.value: "gpt-5",
                RuntimeEventAttribute.LLM_INPUT.value: "hello",
            },
        ),
        _event(
            RuntimeEventName.LLM_COMPLETED,
            base_time + timedelta(seconds=8),
            session_id="session-123",
            snapshot_version=7,
            **{RuntimeEventAttribute.LLM_NAME.value: "gpt-5"},
        ),
    ]

    materializer.apply_events(events)
    snapshot = materializer.get_snapshot("session-123")

    assert snapshot is not None
    assert snapshot["topology_version"] == 5
    assert snapshot["status"] == "active"
    assert snapshot["last_event_name"] == RuntimeEventName.LLM_COMPLETED.value

    nodes = {node["id"]: node for node in snapshot["nodes"]}
    assert nodes["planner"]["status"] == "completed"
    assert nodes["planner"]["version"] == 5

    edges = {edge["id"]: edge for edge in snapshot["edges"]}
    assert edges["agent_handoff:planner->executor"]["status"] == "observed"
    assert edges["a2a:planner->executor"]["status"] == "received"
    assert edges["a2a:planner->executor"]["message_id"] == "msg-1"
    assert edges["a2a:planner->executor"]["fork_id"] == "fork-1"

    tools = {tool["name"]: tool for tool in snapshot["tools"]}
    assert tools["search"]["active_count"] == 0
    assert tools["search"]["started_count"] == 1
    assert tools["search"]["completed_count"] == 1
    assert tools["search"]["status"] == "idle"

    llms = {llm["name"]: llm for llm in snapshot["llms"]}
    assert llms["gpt-5"]["active_count"] == 0
    assert llms["gpt-5"]["started_count"] == 1
    assert llms["gpt-5"]["completed_count"] == 1
    assert llms["gpt-5"]["status"] == "idle"
    assert llms["gpt-5"]["last_input"] == "hello"


def test_materializer_ignores_stale_topology_updates():
    materializer = SessionStateMaterializer()
    base_time = datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc)

    materializer.apply_event(
        _event(
            RuntimeEventName.TOPOLOGY_NODE_COMPLETED,
            base_time + timedelta(seconds=2),
            session_id="session-123",
            snapshot_version=3,
            **{RuntimeEventAttribute.AGENT_NAME.value: "planner"},
        )
    )
    materializer.apply_event(
        _event(
            RuntimeEventName.TOPOLOGY_NODE_STARTED,
            base_time + timedelta(seconds=1),
            session_id="session-123",
            snapshot_version=2,
            **{RuntimeEventAttribute.AGENT_NAME.value: "planner"},
        )
    )

    snapshot = materializer.get_snapshot("session-123")

    assert snapshot is not None
    planner = snapshot["nodes"][0]
    assert planner["status"] == "completed"
    assert planner["version"] == 3


def test_materializer_tracks_slim_and_mcp_protocol_edges():
    materializer = SessionStateMaterializer()
    base_time = datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc)

    materializer.apply_events(
        [
            _event(
                RuntimeEventName.SLIM_MESSAGE_RECEIVED,
                base_time,
                session_id="session-456",
                snapshot_version=2,
                **{
                    RuntimeEventAttribute.SOURCE_AGENT.value: "planner",
                    RuntimeEventAttribute.TARGET_AGENT.value: "slim://executor",
                    RuntimeEventAttribute.SEQUENCE.value: 3,
                    "operation.name": "get_message_async",
                },
            ),
            _event(
                RuntimeEventName.MCP_MESSAGE_SENT,
                base_time + timedelta(seconds=1),
                session_id="session-456",
                snapshot_version=3,
                **{
                    RuntimeEventAttribute.SOURCE_AGENT.value: "planner",
                    RuntimeEventAttribute.TARGET_AGENT.value: "mcp://math-server",
                    RuntimeEventAttribute.MESSAGE_ID.value: "req-1",
                    RuntimeEventAttribute.FORK_ID.value: "fork-1",
                    "operation.name": "tools/call",
                },
            ),
        ]
    )

    snapshot = materializer.get_snapshot("session-456")

    assert snapshot is not None
    edges = {edge["id"]: edge for edge in snapshot["edges"]}
    assert edges["slim:planner->slim://executor"]["kind"] == "slim_message"
    assert edges["slim:planner->slim://executor"]["status"] == "received"
    assert edges["slim:planner->slim://executor"]["transport"] == "slim"
    assert edges["mcp:planner->mcp://math-server"]["kind"] == "mcp_message"
    assert edges["mcp:planner->mcp://math-server"]["status"] == "sent"
    assert edges["mcp:planner->mcp://math-server"]["transport"] == "mcp"
    assert edges["mcp:planner->mcp://math-server"]["message_id"] == "req-1"
    assert edges["mcp:planner->mcp://math-server"]["fork_id"] == "fork-1"


def test_clickhouse_consumer_polls_and_deduplicates_inclusive_cursor():
    session = _FakeHTTPSession(
        [
            {
                "Timestamp": "2026-06-11 09:00:00.000000000",
                "EventName": RuntimeEventName.TOOL_STARTED.value,
                "ServiceName": "runtime-events-test",
                "LogAttributes": {
                    RuntimeEventAttribute.EVENT_NAME.value: RuntimeEventName.TOOL_STARTED.value,
                    RuntimeEventAttribute.EVENT_TIME.value: "2026-06-11T09:00:00+00:00",
                    RuntimeEventAttribute.SESSION_ID.value: "session-123",
                    RuntimeEventAttribute.SNAPSHOT_VERSION.value: "0",
                    RuntimeEventAttribute.TOOL_NAME.value: "search",
                },
            }
        ]
    )
    source = ClickHouseRuntimeEventSource(
        "http://localhost:8123",
        username="admin",
        password="admin",
        session=session,
    )
    materializer = SessionStateMaterializer()
    consumer = ClickHouseRuntimeEventConsumer(
        source,
        materializer,
        service_name="runtime-events-test",
    )

    first_batch = consumer.poll_once()
    second_batch = consumer.poll_once()
    snapshot = materializer.get_snapshot("session-123")

    assert len(first_batch) == 1
    assert len(second_batch) == 1
    assert consumer.cursor == "2026-06-11 09:00:00.000000000"
    assert snapshot is not None
    assert snapshot["tools"][0]["active_count"] == 1
    assert snapshot["tools"][0]["started_count"] == 1
    assert "ServiceName = 'runtime-events-test'" in session.queries[0]
    assert (
        "Timestamp >= toDateTime64('2026-06-11 09:00:00.000000000', 9)"
        in session.queries[1]
    )


def test_materializer_rejects_stale_tool_events_by_version():
    materializer = SessionStateMaterializer()
    base_time = datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc)

    started = _event(
        RuntimeEventName.TOOL_STARTED,
        base_time,
        session_id="session-tool",
        snapshot_version=5,
        **{RuntimeEventAttribute.TOOL_NAME.value: "search"},
    )
    completed = _event(
        RuntimeEventName.TOOL_COMPLETED,
        base_time + timedelta(seconds=1),
        session_id="session-tool",
        snapshot_version=6,
        **{RuntimeEventAttribute.TOOL_NAME.value: "search"},
    )
    # A stale, re-delivered "started" with an older version must be ignored so
    # the active counter cannot be corrupted.
    stale_started = _event(
        RuntimeEventName.TOOL_STARTED,
        base_time + timedelta(seconds=2),
        session_id="session-tool",
        snapshot_version=4,
        **{RuntimeEventAttribute.TOOL_NAME.value: "search"},
    )

    materializer.apply_event(started)
    materializer.apply_event(completed)
    materializer.apply_event(stale_started)

    snapshot = materializer.get_snapshot("session-tool")
    tool = snapshot["tools"][0]
    assert tool["name"] == "search"
    assert tool["active_count"] == 0
    assert tool["started_count"] == 1
    assert tool["completed_count"] == 1
    assert tool["status"] == "idle"


def test_materializer_evicts_oldest_session_when_over_capacity():
    materializer = SessionStateMaterializer(max_sessions=2)
    base_time = datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc)

    for index in range(3):
        materializer.apply_event(
            _event(
                RuntimeEventName.TOPOLOGY_SESSION_STARTED,
                base_time + timedelta(seconds=index),
                session_id=f"session-{index}",
                snapshot_version=1,
            )
        )

    snapshots = {snapshot["session_id"] for snapshot in materializer.list_snapshots()}
    assert snapshots == {"session-1", "session-2"}
    assert materializer.get_snapshot("session-0") is None


def test_materializer_evicts_expired_session_by_ttl():
    materializer = SessionStateMaterializer(session_ttl_seconds=60)
    old_time = datetime.now(timezone.utc) - timedelta(seconds=600)
    fresh_time = datetime.now(timezone.utc)

    materializer.apply_event(
        _event(
            RuntimeEventName.TOPOLOGY_SESSION_STARTED,
            old_time,
            session_id="stale",
            snapshot_version=1,
        )
    )
    # A new event for a different session triggers TTL eviction of the stale one.
    materializer.apply_event(
        _event(
            RuntimeEventName.TOPOLOGY_SESSION_STARTED,
            fresh_time,
            session_id="fresh",
            snapshot_version=1,
        )
    )

    assert materializer.get_snapshot("stale") is None
    assert materializer.get_snapshot("fresh") is not None


def _event(
    name: RuntimeEventName,
    event_time: datetime,
    *,
    session_id: str,
    snapshot_version: int,
    **attributes: object,
) -> RuntimeEventRecord:
    otel_attributes = build_runtime_event_attributes(
        name,
        session_id=session_id,
        snapshot_version=snapshot_version,
        **attributes,
    )
    otel_attributes[RuntimeEventAttribute.EVENT_TIME.value] = event_time.isoformat()
    return RuntimeEventRecord.from_attributes(otel_attributes)


class _FakeHTTPResponse:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.text = "\n".join(json.dumps(row) for row in rows)

    def raise_for_status(self) -> None:
        return None


class _FakeHTTPSession:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    def get(self, url, *, params, auth, timeout):
        self.queries.append(params["query"])
        return _FakeHTTPResponse(self.rows)
