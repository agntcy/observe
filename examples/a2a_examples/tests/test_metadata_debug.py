import asyncio
import json

import pytest
from a2a.client import minimal_agent_card
from a2a.client.interceptors import BeforeArgs
from a2a.helpers import new_text_message
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.types import SendMessageRequest
from google.protobuf.json_format import MessageToDict
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext

from a2a_examples.metadata_debug import (
    RequestMetadataPrinter,
    print_incoming_metadata,
    print_request_metadata,
)


TRACE_ID = "1352fcaf27e873191676eedad808375c"
SPAN_ID = "97f7b8fbab698241"
TRACEPARENT = f"00-{TRACE_ID}-{SPAN_ID}-01"


def test_prints_propagated_ids_instead_of_active_span_ids(capsys):
    metadata = {
        "observe": {"traceparent": TRACEPARENT, "session_id": "demo-session"},
        "nested": {"values": [1, True, "demo"]},
    }
    active_span = NonRecordingSpan(SpanContext(trace_id=1, span_id=2, is_remote=False))

    with trace.use_span(active_span):
        print_request_metadata(
            metadata, direction="incoming", operation="agent.execute"
        )

    entry = json.loads(capsys.readouterr().out)
    assert entry["metadata"] == metadata
    assert entry["traceparent"] == TRACEPARENT
    assert entry["trace_id"] == TRACE_ID
    assert entry["span_id"] == SPAN_ID
    assert entry["trace_context_status"] == "valid"


@pytest.mark.parametrize(
    ("observe", "status"),
    [
        ({}, "missing"),
        ({"traceparent": "not-a-traceparent"}, "invalid"),
        ({"traceparent": f"00-{'0' * 32}-{'0' * 16}-01"}, "invalid"),
    ],
)
def test_reports_missing_or_invalid_context(observe, status, capsys):
    print_request_metadata(
        {"observe": observe}, direction="outgoing", operation="send_message"
    )

    entry = json.loads(capsys.readouterr().out)
    assert entry["trace_context_status"] == status
    assert entry["trace_id"] is None
    assert entry["span_id"] is None


@pytest.mark.parametrize("operation", ["send_message", "send_message_streaming"])
def test_interceptor_prints_injected_metadata_without_modifying_request(
    operation, capsys
):
    from ioa_observe.sdk.instrumentations.a2a import _inject_observe_metadata

    request = SendMessageRequest(message=new_text_message("demo"))
    span = NonRecordingSpan(
        SpanContext(
            trace_id=int(TRACE_ID, 16),
            span_id=int(SPAN_ID, 16),
            is_remote=False,
            trace_flags=trace.TraceFlags.SAMPLED,
        )
    )
    with trace.use_span(span):
        request = _inject_observe_metadata(request, operation)
    original = request.SerializeToString()
    args = BeforeArgs(
        input=request,
        method=operation,
        agent_card=minimal_agent_card("http://localhost:9999", ["JSONRPC"]),
    )

    asyncio.run(RequestMetadataPrinter().before(args))

    entry = json.loads(capsys.readouterr().out)
    assert entry["operation"] == operation
    assert entry["direction"] == "outgoing"
    assert entry["metadata"] == MessageToDict(request.message.metadata)
    assert entry["metadata_location"] == "message.metadata"
    assert entry["trace_id"] == TRACE_ID
    assert entry["span_id"] == SPAN_ID
    assert request.SerializeToString() == original


@pytest.mark.parametrize("location", ["message", "request"])
def test_receiver_prints_metadata_from_both_supported_locations(location, capsys):
    request = SendMessageRequest(message=new_text_message("demo"))
    metadata = request.message.metadata if location == "message" else request.metadata
    metadata.update({"observe": {"traceparent": TRACEPARENT}})
    context = RequestContext(call_context=ServerCallContext(), request=request)
    original = request.SerializeToString()

    print_incoming_metadata(context)

    entry = json.loads(capsys.readouterr().out)
    assert entry["direction"] == "incoming"
    assert entry["metadata_location"] == f"{location}.metadata"
    assert entry["trace_id"] == TRACE_ID
    assert entry["span_id"] == SPAN_ID
    assert request.SerializeToString() == original


def test_interceptor_ignores_non_message_operations(capsys):
    args = BeforeArgs(
        input="task-id",
        method="get_task",
        agent_card=minimal_agent_card("http://localhost:9999", ["JSONRPC"]),
    )

    asyncio.run(RequestMetadataPrinter().before(args))

    assert capsys.readouterr().out == ""
