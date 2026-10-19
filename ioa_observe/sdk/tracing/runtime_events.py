# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from ioa_observe.sdk.utils.const import OBSERVE_AGENT_SPAN_ID, OBSERVE_AGENT_TRACE_ID


GEN_AI_MODEL_OPERATIONS = frozenset({"chat", "generate_content", "text_completion"})


class RuntimeEventName(str, Enum):
    TOPOLOGY_SESSION_STARTED = "topology.session.started"
    TOPOLOGY_SESSION_COMPLETED = "topology.session.completed"
    TOPOLOGY_NODE_STARTED = "topology.node.started"
    TOPOLOGY_NODE_COMPLETED = "topology.node.completed"
    TOPOLOGY_EDGE_UPDATED = "topology.edge.updated"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    LLM_STARTED = "llm.started"
    LLM_COMPLETED = "llm.completed"
    A2A_MESSAGE_SENT = "a2a.message.sent"
    A2A_MESSAGE_RECEIVED = "a2a.message.received"
    A2A_MESSAGE_COMPLETED = "a2a.message.completed"
    SLIM_MESSAGE_SENT = "slim.message.sent"
    SLIM_MESSAGE_RECEIVED = "slim.message.received"
    MCP_MESSAGE_SENT = "mcp.message.sent"
    MCP_MESSAGE_RECEIVED = "mcp.message.received"
    MCP_MESSAGE_COMPLETED = "mcp.message.completed"
    MCP_TOOLS_DISCOVERED = "mcp.tools.discovered"
    MCP_TOOLS_PUBLISHED = "mcp.tools.published"
    A2A_AGENT_CARD = "a2a.agent_card"


CAPABILITY_EVENT_NAMES = frozenset(
    {
        RuntimeEventName.MCP_TOOLS_DISCOVERED.value,
        RuntimeEventName.MCP_TOOLS_PUBLISHED.value,
        RuntimeEventName.A2A_AGENT_CARD.value,
    }
)


class RuntimeEventAttribute(str, Enum):
    EVENT_ID = "event.id"
    EVENT_PARENT_ID = "event.parent.id"
    EVENT_NAME = "event.name"
    EVENT_TIME = "event.time"
    SESSION_ID = "session.id"
    APPLICATION_ID = "application_id"
    SNAPSHOT_VERSION = "snapshot.version"
    AGENT_NAME = "agent.name"
    AGENT_INPUT = "agent.input"
    AGENT_OUTPUT = "agent.output"
    TOOL_NAME = "tool.name"
    TOOL_INPUT = "tool.input"
    TOOL_OUTPUT = "tool.output"
    TOOL_STATUS = "tool.status"
    TOOL_ERROR_MESSAGE = "tool.error.message"
    LLM_NAME = "llm.name"
    LLM_INPUT = "llm.input"
    LLM_OUTPUT = "llm.output"
    LLM_CALL_ID = "llm.call.id"
    SOURCE_AGENT = "source.agent"
    SOURCE_AGENT_SPAN_ID = "source.agent.span.id"
    SOURCE_AGENT_TRACE_ID = "source.agent.trace.id"
    TARGET_AGENT = "target.agent"
    MESSAGE_ID = "message.id"
    MESSAGE_INPUT = "message.input"
    MESSAGE_OUTPUT = "message.output"
    FORK_ID = "fork.id"
    SEQUENCE = "sequence"


COMMON_REQUIRED_ATTRIBUTES = frozenset(
    {
        RuntimeEventAttribute.EVENT_NAME.value,
        RuntimeEventAttribute.EVENT_TIME.value,
        RuntimeEventAttribute.SESSION_ID.value,
        RuntimeEventAttribute.SNAPSHOT_VERSION.value,
    }
)


EVENT_REQUIRED_ATTRIBUTES = {
    **{
        name: (COMMON_REQUIRED_ATTRIBUTES - {"session.id"})
        | {"capability.source", "capability.capture.state"}
        for name in CAPABILITY_EVENT_NAMES
    },
    RuntimeEventName.TOPOLOGY_SESSION_STARTED.value: COMMON_REQUIRED_ATTRIBUTES,
    RuntimeEventName.TOPOLOGY_SESSION_COMPLETED.value: COMMON_REQUIRED_ATTRIBUTES,
    RuntimeEventName.TOPOLOGY_NODE_STARTED.value: COMMON_REQUIRED_ATTRIBUTES
    | {RuntimeEventAttribute.AGENT_NAME.value},
    RuntimeEventName.TOPOLOGY_NODE_COMPLETED.value: COMMON_REQUIRED_ATTRIBUTES
    | {RuntimeEventAttribute.AGENT_NAME.value},
    RuntimeEventName.TOPOLOGY_EDGE_UPDATED.value: COMMON_REQUIRED_ATTRIBUTES
    | {
        RuntimeEventAttribute.SOURCE_AGENT.value,
        RuntimeEventAttribute.TARGET_AGENT.value,
    },
    RuntimeEventName.TOOL_STARTED.value: COMMON_REQUIRED_ATTRIBUTES
    | {RuntimeEventAttribute.TOOL_NAME.value},
    RuntimeEventName.TOOL_COMPLETED.value: COMMON_REQUIRED_ATTRIBUTES
    | {RuntimeEventAttribute.TOOL_NAME.value},
    RuntimeEventName.LLM_STARTED.value: COMMON_REQUIRED_ATTRIBUTES
    | {RuntimeEventAttribute.LLM_NAME.value},
    RuntimeEventName.LLM_COMPLETED.value: COMMON_REQUIRED_ATTRIBUTES
    | {RuntimeEventAttribute.LLM_NAME.value},
    RuntimeEventName.A2A_MESSAGE_SENT.value: COMMON_REQUIRED_ATTRIBUTES
    | {
        RuntimeEventAttribute.SOURCE_AGENT.value,
        RuntimeEventAttribute.TARGET_AGENT.value,
    },
    RuntimeEventName.A2A_MESSAGE_RECEIVED.value: COMMON_REQUIRED_ATTRIBUTES
    | {
        RuntimeEventAttribute.SOURCE_AGENT.value,
        RuntimeEventAttribute.TARGET_AGENT.value,
    },
    RuntimeEventName.A2A_MESSAGE_COMPLETED.value: COMMON_REQUIRED_ATTRIBUTES
    | {
        RuntimeEventAttribute.SOURCE_AGENT.value,
        RuntimeEventAttribute.TARGET_AGENT.value,
    },
    RuntimeEventName.SLIM_MESSAGE_SENT.value: COMMON_REQUIRED_ATTRIBUTES
    | {
        RuntimeEventAttribute.SOURCE_AGENT.value,
        RuntimeEventAttribute.TARGET_AGENT.value,
    },
    RuntimeEventName.SLIM_MESSAGE_RECEIVED.value: COMMON_REQUIRED_ATTRIBUTES
    | {
        RuntimeEventAttribute.SOURCE_AGENT.value,
        RuntimeEventAttribute.TARGET_AGENT.value,
    },
    RuntimeEventName.MCP_MESSAGE_SENT.value: COMMON_REQUIRED_ATTRIBUTES
    | {
        RuntimeEventAttribute.SOURCE_AGENT.value,
        RuntimeEventAttribute.TARGET_AGENT.value,
    },
    RuntimeEventName.MCP_MESSAGE_RECEIVED.value: COMMON_REQUIRED_ATTRIBUTES
    | {
        RuntimeEventAttribute.SOURCE_AGENT.value,
        RuntimeEventAttribute.TARGET_AGENT.value,
    },
    RuntimeEventName.MCP_MESSAGE_COMPLETED.value: COMMON_REQUIRED_ATTRIBUTES
    | {
        RuntimeEventAttribute.SOURCE_AGENT.value,
        RuntimeEventAttribute.TARGET_AGENT.value,
    },
}


OTEL_ATTRIBUTE_VALUE_TYPES = (str, bool, int, float)


@dataclass(frozen=True)
class RuntimeEvent:
    name: RuntimeEventName | str
    session_id: str | None
    snapshot_version: int
    event_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    attributes: Mapping[str, Any] = field(default_factory=dict)
    trace_id: int | None = None
    span_id: int | None = None
    trace_flags: int | None = None
    instrumentation_scope_name: str | None = None
    instrumentation_scope_version: str | None = None

    def to_otel_attributes(self) -> dict[str, str | bool | int | float]:
        event_name = (
            self.name.value if isinstance(self.name, RuntimeEventName) else self.name
        )
        attributes: dict[str, str | bool | int | float] = {
            RuntimeEventAttribute.EVENT_NAME.value: event_name,
            RuntimeEventAttribute.EVENT_TIME.value: self.event_time.isoformat(),
            RuntimeEventAttribute.SNAPSHOT_VERSION.value: self.snapshot_version,
            **span_correlation_attributes(
                self.attributes, trace_id=self.trace_id, span_id=self.span_id
            ),
        }
        if self.session_id is not None:
            attributes[RuntimeEventAttribute.SESSION_ID.value] = self.session_id

        for key, value in self.attributes.items():
            if value is None:
                continue
            attributes[str(key)] = _to_otel_attribute_value(value)

        validate_runtime_event_attributes(event_name, attributes)
        return attributes


def identify_runtime_event(
    attributes: Mapping[str, str | bool | int | float],
) -> dict[str, str | bool | int | float]:
    event = {
        key: value
        for key, value in attributes.items()
        if key != RuntimeEventAttribute.EVENT_ID.value
    }
    payload = json.dumps(
        event, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
    event[RuntimeEventAttribute.EVENT_ID.value] = hashlib.sha256(payload).hexdigest()
    return event


def span_correlation_attributes(
    attributes: Mapping[str, Any],
    *,
    trace_id: int | None = None,
    span_id: int | None = None,
) -> dict[str, str]:
    correlation = {
        key: value
        for key in (
            RuntimeEventAttribute.APPLICATION_ID.value,
            OBSERVE_AGENT_SPAN_ID,
            OBSERVE_AGENT_TRACE_ID,
        )
        if isinstance(value := attributes.get(key), str) and value
    }
    if trace_id:
        correlation["trace.id"] = f"{trace_id:032x}"
    if span_id:
        correlation["span.id"] = f"{span_id:016x}"
    return correlation


def build_runtime_event_attributes(
    name: RuntimeEventName | str,
    session_id: str | None,
    snapshot_version: int,
    **attributes: Any,
) -> dict[str, str | bool | int | float]:
    return RuntimeEvent(
        name=name,
        session_id=session_id,
        snapshot_version=snapshot_version,
        attributes=attributes,
    ).to_otel_attributes()


def validate_runtime_event_attributes(
    event_name: str,
    attributes: Mapping[str, Any],
) -> None:
    required_attributes = EVENT_REQUIRED_ATTRIBUTES.get(event_name)
    if required_attributes is None:
        raise ValueError(f"Unknown runtime event name: {event_name}")

    missing = sorted(
        attribute for attribute in required_attributes if attribute not in attributes
    )
    if missing:
        raise ValueError(
            f"Runtime event {event_name} missing required attributes: "
            + ", ".join(missing)
        )


def _to_otel_attribute_value(value: Any) -> str | bool | int | float:
    if isinstance(value, OTEL_ATTRIBUTE_VALUE_TYPES):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
