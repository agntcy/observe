# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from opentelemetry import baggage, context, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.tracing import get_current_traceparent
from ioa_observe.sdk.tracing.context_utils import (
    _get_outbound_session_id,
    _inject_outbound_context,
)


INCOMING = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"


def test_outbound_context_uses_valid_unsampled_span_without_changing_stored_lookup():
    span_context = SpanContext(
        trace_id=0x11111111111111111111111111111111,
        span_id=0x2222222222222222,
        is_remote=False,
        trace_flags=TraceFlags(0),
        trace_state=TraceState([("vendor", "active")]),
    )
    ctx = trace.set_span_in_context(NonRecordingSpan(span_context))
    ctx = context.set_value("current_traceparent", INCOMING, context=ctx)
    ctx = context.set_value("session.id", "active-session", context=ctx)
    ctx = baggage.set_baggage("local", "new", context=ctx)
    ctx = baggage.set_baggage("shared", "local", context=ctx)
    token = context.attach(ctx)
    try:
        metadata = {
            "traceparent": INCOMING,
            "tracestate": "vendor=old",
            "baggage": "existing=kept,shared=wire",
            "custom": {"preserved": True},
        }
        assert _inject_outbound_context(metadata) is metadata
        assert metadata["traceparent"] == (
            "00-11111111111111111111111111111111-2222222222222222-00"
        )
        assert metadata["tracestate"] == "vendor=active"
        assert metadata["custom"] == {"preserved": True}
        extracted = baggage.get_all(W3CBaggagePropagator().extract(metadata))
        assert extracted == {"existing": "kept", "shared": "local", "local": "new"}
        assert get_current_traceparent() == INCOMING
        kv_store.set(f"execution.{INCOMING}", "old-session")
        assert _get_outbound_session_id() == "active-session"
    finally:
        context.detach(token)
        kv_store.delete(f"execution.{INCOMING}")


def test_outbound_session_uses_legacy_incoming_mapping():
    token = context.attach(context.set_value("current_traceparent", INCOMING))
    kv_store.set(f"execution.{INCOMING}", "legacy-session")
    try:
        assert _get_outbound_session_id() == "legacy-session"
        metadata = {"tracestate": "vendor=kept", "baggage": "existing=kept"}
        _inject_outbound_context(metadata)
        assert metadata == {"tracestate": "vendor=kept", "baggage": "existing=kept"}
    finally:
        context.detach(token)
        kv_store.delete(f"execution.{INCOMING}")


def test_outbound_context_retains_wire_baggage_properties_and_encoding():
    carrier = {"baggage": "existing=hello%20world;property,shared=wire"}
    _inject_outbound_context(carrier)
    assert carrier["baggage"] == "existing=hello%20world;property,shared=wire"
    token = context.attach(baggage.set_baggage("shared", "local"))
    try:
        _inject_outbound_context(carrier)
        assert carrier["baggage"] == "existing=hello%20world;property,shared=local"
    finally:
        context.detach(token)
