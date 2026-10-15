# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import asyncio
import inspect
import sys
from types import ModuleType, SimpleNamespace

import pytest
from opentelemetry import baggage, context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel

pytest.importorskip("a2a")
from a2a.client.base_client import BaseClient
from a2a.types import Message, SendMessageRequest

from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.instrumentations import a2a
from ioa_observe.sdk.tracing import (
    clear_runtime_event_listeners,
    register_runtime_event_listener,
)
from ioa_observe.sdk.tracing.topology import clear_topology_listeners
from ioa_observe.sdk import TracerWrapper


INCOMING = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
PATHS = (
    "base_coroutine",
    "base_asyncgen",
    "base_streaming",
    "legacy_send",
    "legacy_broadcast",
    "slim_send",
    "slim_streaming",
)


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(TracerWrapper, "endpoint", None)
    clear_runtime_event_listeners()
    clear_topology_listeners()
    with kv_store._lock:
        kv_store.store.clear()
    yield
    clear_runtime_event_listeners()
    clear_topology_listeners()
    with kv_store._lock:
        kv_store.store.clear()


def make_request(legacy, message_id):
    metadata = {
        "unrelated": {"kept": True},
        "observe": {
            "traceparent": INCOMING,
            "baggage": "existing=kept",
            "tracestate": "vendor=kept",
            "custom": "kept",
        },
    }
    if legacy:

        class LegacyMessage(BaseModel):
            messageId: str | None = None

        class Params(BaseModel):
            metadata: dict
            message: LegacyMessage

        class Request(BaseModel):
            params: Params

        return Request(
            params=Params(
                metadata=metadata, message=LegacyMessage(messageId=message_id)
            )
        )
    # A2A 1.x uses protobufs; earlier SDKs use Pydantic models.
    if hasattr(SendMessageRequest, "DESCRIPTOR"):
        return SendMessageRequest(
            metadata={"outer": "kept"},
            message=Message(message_id=message_id or "", metadata=metadata),
        )
    from a2a.types import MessageSendParams

    return SendMessageRequest(
        id="request-1",
        params=MessageSendParams(
            metadata=metadata,
            message=Message(role="user", messageId=message_id or "native", parts=[]),
        ),
    )


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("sampled", [True, False])
@pytest.mark.parametrize("legacy_session", [True, False])
def test_a2a_outbound_wire_matches_send_event_and_restores_methods(
    monkeypatch, path, sampled, legacy_session
):
    provider = TracerProvider(sampler=ALWAYS_ON if sampled else ALWAYS_OFF)
    instrumentor = a2a.A2AInstrumentor()
    monkeypatch.setattr(a2a, "_global_tracer", provider.get_tracer(__name__))
    monkeypatch.setattr(a2a, "_original_methods", {})
    received = []
    events = []
    register_runtime_event_listener(events.append)

    async def send(self, request, *args, **kwargs):
        received.append(
            (request, trace.get_current_span().get_span_context(), args, kwargs)
        )
        return "response"

    async def stream(self, request, *args, **kwargs):
        received.append(
            (request, trace.get_current_span().get_span_context(), args, kwargs)
        )
        yield "first"
        yield "second"

    streaming = path in ("base_asyncgen", "base_streaming", "slim_streaming")
    original = stream if streaming else send
    legacy = path.startswith("legacy")
    if path.startswith("base"):
        cls = BaseClient
        method = (
            "send_message_streaming" if path == "base_streaming" else "send_message"
        )
        monkeypatch.setattr(cls, method, original, raising=False)
        instrumentor._instrument_base_client()
    elif legacy:
        # Legacy clients were removed in SDK 1.x. Exercise their historical
        # Pydantic request shape and import path without installing an old SDK.
        cls = type("A2AClient", (), {"send_message": send, "broadcast_message": send})
        module = ModuleType("a2a.client.legacy")
        module.A2AClient = cls
        monkeypatch.setitem(sys.modules, "a2a.client.legacy", module)
        method = "broadcast_message" if path == "legacy_broadcast" else "send_message"
        instrumentor._instrument_legacy_client()
    else:
        pytest.importorskip("slima2a")
        from slima2a.client_transport import SRPCTransport

        cls = SRPCTransport
        method = "send_message_streaming" if streaming else "send_message"
        monkeypatch.setattr(cls, method, original)
        instrumentor._instrument_slima2a()

    request = make_request(legacy, None if not sampled else "message-1")
    kv_store.set(f"execution.{INCOMING}", "legacy-session")
    ctx = TraceContextTextMapPropagator().extract({"traceparent": INCOMING})
    ctx = context.set_value("current_traceparent", INCOMING, context=ctx)
    if not legacy_session:
        ctx = context.set_value("session.id", "active-session", context=ctx)
    ctx = baggage.set_baggage("local", "added", context=ctx)
    token = context.attach(ctx)
    parent = trace.get_current_span().get_span_context()
    expected_session = "legacy-session" if legacy_session else "active-session"
    kv_store.set(f"session.{expected_session}.last_agent_span_id", "0000000000000123")
    kv_store.set(
        f"session.{expected_session}.last_agent_trace_id",
        "00000000000000000000000000000456",
    )

    async def invoke():
        result = getattr(cls, method)(
            SimpleNamespace(name="receiver"), request, "arg", option=1
        )
        if streaming:
            assert inspect.isasyncgen(result)
            assert received == []
            assert events == []
            return [item async for item in result]
        return await result

    try:
        expected = ["first", "second"] if streaming else "response"
        assert asyncio.run(invoke()) == expected
    finally:
        context.detach(token)
    metadata = a2a._safe_get_metadata(received[0][0])
    observe = metadata["observe"]
    sent = next(event for event in events if event["event.name"] == "a2a.message.sent")
    assert observe["event.id"] == sent["event.id"]
    assert observe["traceparent"] != INCOMING
    _, trace_id, span_id, flags = observe["traceparent"].split("-")
    assert sent["trace.id"] == trace_id
    assert sent["span.id"] == span_id
    assert trace_id == INCOMING.split("-")[1]
    assert span_id != INCOMING.split("-")[2]
    assert bool(int(flags, 16) & 1) is sampled
    assert observe["session_id"] == expected_session
    assert sent["source.agent.span.id"] == observe["last_agent_span_id"]
    assert sent["source.agent.trace.id"] == observe["last_agent_trace_id"]
    assert sent["source.agent.span.id"] != sent["span.id"]
    assert kv_store.get(f"execution.{observe['traceparent']}") == expected_session
    assert metadata["unrelated"] == {"kept": True}
    assert observe["custom"] == "kept"
    assert observe["tracestate"] == "vendor=kept"
    assert "existing=kept" in observe["baggage"]
    assert "local=added" in observe["baggage"]
    assert received[0][1] == parent  # Network scope/lifetime is unchanged.
    assert received[0][2:] == (("arg",), {"option": 1})
    if not sampled:
        assert "message.id" not in sent
    else:
        assert sent["message.id"] == "message-1"
    assert not any(key.startswith("source.") for key in observe)
    instrumentor._uninstrument()
    assert getattr(cls, method) is original
    provider.shutdown()
