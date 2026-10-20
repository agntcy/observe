# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0
"""Runtime events for a request that is about to be sent.

``llm.started`` used to be emitted when the model had answered, and nothing carried the text of an A2A
message or the arguments of an MCP ``tools/call`` until the peer had already received them, so a consumer
of the realtime events could not act on a request before it was made. These helpers emit the event while
the request is still local: the span that is current is the span the request's trace context carries, so the
event is tied to the request by that span.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ioa_observe.sdk.tracing.runtime_event_emitter import emit_runtime_event
from ioa_observe.sdk.tracing.runtime_events import (
    RuntimeEventAttribute,
    RuntimeEventName,
    build_runtime_event_attributes,
)
from ioa_observe.sdk.tracing.topology import next_session_event_version

logger = logging.getLogger(__name__)

# Carrier key (A2A ``metadata.observe``, MCP ``params._meta``) holding the ``event.id`` of the pre-call
# ``tool.started`` event. The carrier's ``event.id`` keeps naming the ``*.message.sent`` event.
PRECALL_EVENT_ID_KEY = "precall.event.id"


def emit_tool_started_before_send(
    session_id: str,
    tool_name: str,
    tool_input: Any,
    *,
    call_id: str | None = None,
    agent_name: str | None = None,
    parent_event_id: str | None = None,
) -> str | None:
    """Emit ``tool.started`` with the input of a call that has not been sent yet. Never raises.

    Returns the ``event.id`` of the emitted event, or ``None`` when nothing was emitted."""
    try:
        attributes: dict[str, Any] = {
            RuntimeEventAttribute.TOOL_NAME.value: tool_name,
            RuntimeEventAttribute.TOOL_INPUT.value: tool_input
            if isinstance(tool_input, str)
            else json.dumps(tool_input, sort_keys=True, default=str),
        }
        if call_id:
            attributes["tool.call.id"] = call_id
        if agent_name:
            attributes[RuntimeEventAttribute.AGENT_NAME.value] = agent_name
        if parent_event_id:
            # The outgoing ``*.message.sent`` event of the same request, emitted just before this one.
            attributes[RuntimeEventAttribute.EVENT_PARENT_ID.value] = parent_event_id
        event = emit_runtime_event(
            build_runtime_event_attributes(
                RuntimeEventName.TOOL_STARTED,
                session_id=session_id,
                snapshot_version=next_session_event_version(session_id),
                **attributes,
            )
        )
        event_id = event.get("event.id") if event else None
        return event_id if isinstance(event_id, str) and event_id else None
    except Exception:  # an event must never break the request it describes
        logger.exception("pre-call tool.started event failed")
        return None
