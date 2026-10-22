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

from opentelemetry import trace

from ioa_observe.sdk.config import is_content_tracing_enabled
from ioa_observe.sdk.tracing.message_payloads import serialize_message_payload
from ioa_observe.sdk.tracing.tool_results import tool_error_message
from ioa_observe.sdk.tracing.runtime_event_emitter import emit_runtime_event
from ioa_observe.sdk.tracing.runtime_events import (
    RuntimeEventAttribute,
    RuntimeEventName,
    build_runtime_event_attributes,
)
from ioa_observe.sdk.tracing.topology import next_session_event_version

logger = logging.getLogger(__name__)


def emit_tool_started_before_send(
    session_id: str,
    tool_name: str,
    tool_input: Any,
    *,
    call_id: str | None = None,
    agent_name: str | None = None,
    parent_event_id: str | None = None,
    tool_type: str | None = None,
) -> dict[str, Any] | None:
    """Emit ``tool.started`` with the input of a call that has not been sent yet. Never raises."""
    try:
        attributes: dict[str, Any] = {
            RuntimeEventAttribute.TOOL_NAME.value: tool_name,
            RuntimeEventAttribute.TOOL_INPUT.value: tool_input
            if isinstance(tool_input, str)
            else json.dumps(tool_input, sort_keys=True, default=str),
        }
        if tool_type is not None:
            attributes[RuntimeEventAttribute.TOOL_TYPE.value] = tool_type
        if call_id:
            attributes["tool.call.id"] = call_id
        if agent_name:
            attributes[RuntimeEventAttribute.AGENT_NAME.value] = agent_name
        if parent_event_id:
            # The outgoing ``*.message.sent`` event of the same request, emitted just before this one.
            attributes[RuntimeEventAttribute.EVENT_PARENT_ID.value] = parent_event_id
        attributes["tool.execution.kind"] = "client"
        return emit_runtime_event(
            build_runtime_event_attributes(
                RuntimeEventName.TOOL_STARTED,
                session_id=session_id,
                snapshot_version=next_session_event_version(session_id),
                **attributes,
            )
        )
    except Exception:  # an event must never break the request it describes
        logger.exception("pre-call tool.started event failed")


def emit_tool_completed_after_send(
    started: dict[str, Any] | None,
    *,
    result: Any = None,
    error: BaseException | None = None,
    span: trace.Span | None = None,
) -> None:
    """Close a client-side tool invocation on the same span as its start."""
    if started is None:
        return
    try:
        error_message = (
            (str(error) or type(error).__name__)
            if error is not None
            else tool_error_message(result)
        )
        explicit_error = (
            result.get("isError", result.get("is_error", False))
            if isinstance(result, dict)
            else getattr(result, "isError", getattr(result, "is_error", False))
        )
        attributes = {
            key: started[key]
            for key in (
                "tool.name",
                RuntimeEventAttribute.TOOL_TYPE.value,
                "tool.call.id",
                "agent.name",
                "tool.execution.kind",
                "ioa_observe.agent.span_id",
                "ioa_observe.agent.trace_id",
                "application_id",
            )
            if key in started
        }
        attributes["event.parent.id"] = started["event.id"]
        attributes["tool.status"] = (
            "error" if error_message or explicit_error else "success"
        )
        if error is not None:
            attributes["error.type"] = type(error).__name__
        if is_content_tracing_enabled():
            if "tool.input" in started:
                attributes["tool.input"] = started["tool.input"]
            if error_message:
                attributes["tool.error.message"] = error_message
            output = serialize_message_payload(result, protocol="tool")
            if output is not None:
                attributes["tool.output"] = output
        emit_runtime_event(
            build_runtime_event_attributes(
                RuntimeEventName.TOOL_COMPLETED,
                session_id=started["session.id"],
                snapshot_version=next_session_event_version(started["session.id"]),
                **attributes,
            ),
            span=span,
        )
    except Exception:
        logger.exception("client tool.completed event failed")
