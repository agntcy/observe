# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


class RuntimeEventName(str, Enum):
    TOPOLOGY_SESSION_STARTED = "topology.session.started"
    TOPOLOGY_NODE_STARTED = "topology.node.started"
    TOPOLOGY_NODE_COMPLETED = "topology.node.completed"
    TOPOLOGY_EDGE_UPDATED = "topology.edge.updated"
    A2A_MESSAGE_SENT = "a2a.message.sent"
    A2A_MESSAGE_RECEIVED = "a2a.message.received"


class RuntimeEventAttribute(str, Enum):
    EVENT_NAME = "event.name"
    EVENT_TIME = "event.time"
    SESSION_ID = "session.id"
    SNAPSHOT_VERSION = "snapshot.version"
    AGENT_NAME = "agent.name"
    SOURCE_AGENT = "source.agent"
    TARGET_AGENT = "target.agent"
    MESSAGE_ID = "message.id"
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
    RuntimeEventName.TOPOLOGY_SESSION_STARTED.value: COMMON_REQUIRED_ATTRIBUTES,
    RuntimeEventName.TOPOLOGY_NODE_STARTED.value: COMMON_REQUIRED_ATTRIBUTES
    | {RuntimeEventAttribute.AGENT_NAME.value},
    RuntimeEventName.TOPOLOGY_NODE_COMPLETED.value: COMMON_REQUIRED_ATTRIBUTES
    | {RuntimeEventAttribute.AGENT_NAME.value},
    RuntimeEventName.TOPOLOGY_EDGE_UPDATED.value: COMMON_REQUIRED_ATTRIBUTES
    | {
        RuntimeEventAttribute.SOURCE_AGENT.value,
        RuntimeEventAttribute.TARGET_AGENT.value,
    },
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
}


OTEL_ATTRIBUTE_VALUE_TYPES = (str, bool, int, float)


@dataclass(frozen=True)
class RuntimeEvent:
    name: RuntimeEventName | str
    session_id: str
    snapshot_version: int
    event_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def to_otel_attributes(self) -> dict[str, str | bool | int | float]:
        event_name = (
            self.name.value if isinstance(self.name, RuntimeEventName) else self.name
        )
        attributes: dict[str, str | bool | int | float] = {
            RuntimeEventAttribute.EVENT_NAME.value: event_name,
            RuntimeEventAttribute.EVENT_TIME.value: self.event_time.isoformat(),
            RuntimeEventAttribute.SESSION_ID.value: self.session_id,
            RuntimeEventAttribute.SNAPSHOT_VERSION.value: self.snapshot_version,
        }

        for key, value in self.attributes.items():
            if value is None:
                continue
            attributes[str(key)] = _to_otel_attribute_value(value)

        validate_runtime_event_attributes(event_name, attributes)
        return attributes


def build_runtime_event_attributes(
    name: RuntimeEventName | str,
    session_id: str,
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
