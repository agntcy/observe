# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import asyncio
import copy
import inspect
import json
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


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("copied", [False, True])
def test_nested_a2a_client_transport_emits_one_message_lifecycle(
    monkeypatch, streaming, copied
):
    pytest.importorskip("slima2a")
    from slima2a.client_transport import SRPCTransport

    provider = TracerProvider()
    instrumentor = a2a.A2AInstrumentor()
    monkeypatch.setattr(a2a, "_global_tracer", provider.get_tracer(__name__))
    monkeypatch.setattr(a2a, "_original_methods", {})
    events = []
    register_runtime_event_listener(events.append)
    transport = SimpleNamespace(_card=SimpleNamespace(name="executor"))

    async def send(self, request, *args, **kwargs):
        return {"answer": "done"}

    async def stream(self, request, *args, **kwargs):
        yield {"answer": "done"}

    async def outer_send(self, request, *args, **kwargs):
        if copied:
            request = copy.deepcopy(request)
        return await SRPCTransport.send_message(transport, request, *args, **kwargs)

    async def outer_stream(self, request, *args, **kwargs):
        if copied:
            request = copy.deepcopy(request)
        async for item in SRPCTransport.send_message_streaming(
            transport, request, *args, **kwargs
        ):
            yield item

    monkeypatch.setattr(SRPCTransport, "send_message", send)
    monkeypatch.setattr(SRPCTransport, "send_message_streaming", stream)
    monkeypatch.setattr(
        BaseClient, "send_message", outer_stream if streaming else outer_send
    )
    instrumentor._instrument_base_client()
    instrumentor._instrument_slima2a()

    async def invoke():
        token = context.attach(context.set_value("session.id", "nested-session"))
        try:
            result = BaseClient.send_message(
                SimpleNamespace(_card=SimpleNamespace(name="executor")),
                make_request(False, "nested-message"),
            )
            return [item async for item in result] if streaming else await result
        finally:
            context.detach(token)

    try:
        assert asyncio.run(invoke()) == (
            [{"answer": "done"}] if streaming else {"answer": "done"}
        )
        names = [event["event.name"] for event in events]
        assert names.count("a2a.message.sent") == 1
        assert names.count("a2a.message.completed") == 1
        assert names.count("a2a.agent_card") == 1
        [card] = [event for event in events if event["event.name"] == "a2a.agent_card"]
        assert card["capability.observation.kind"] == "message-context"
    finally:
        instrumentor._uninstrument()
        provider.shutdown()


@pytest.mark.parametrize("streaming", [False, True])
def test_nested_guard_does_not_suppress_retries_or_concurrent_calls(streaming):
    calls = []
    client = SimpleNamespace(name="executor")

    async def raw_send(instance, request):
        await asyncio.sleep(0)
        raise RuntimeError("retry")

    async def raw_stream(instance, request):
        await asyncio.sleep(0)
        yield "partial"

    async def send(instance, request):
        calls.append(request)
        return await raw_send(instance, request)

    async def stream(instance, request):
        calls.append(request)
        async for item in raw_stream(instance, request):
            yield item

    wrapped = a2a._guard_nested_send(
        raw_stream if streaming else raw_send,
        stream if streaming else send,
        streaming=streaming,
    )
    request = make_request(False, "reused-message")

    async def invoke():
        if streaming:
            pending = wrapped(client, request)
            assert await anext(pending) == "partial"
            assert [item async for item in wrapped(client, request)] == ["partial"]
            await pending.aclose()

            async def consume():
                return [item async for item in wrapped(client, copy.deepcopy(request))]

            await asyncio.gather(
                consume(),
                consume(),
            )
        else:
            for _ in range(2):
                with pytest.raises(RuntimeError, match="retry"):
                    await wrapped(client, request)
            results = await asyncio.gather(
                wrapped(client, copy.deepcopy(request)),
                wrapped(client, copy.deepcopy(request)),
                return_exceptions=True,
            )
            assert all(isinstance(result, RuntimeError) for result in results)
        assert a2a._active_sends.get() == ()

    asyncio.run(invoke())
    assert len(calls) == 4


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    from ioa_observe.sdk.tracing import runtime_event_emitter

    monkeypatch.setattr(runtime_event_emitter, "_emit_to_otel_logs", lambda *args: None)
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
    [completed] = [
        event for event in events if event["event.name"] == "a2a.message.completed"
    ]
    assert completed["event.parent.id"] == sent["event.id"]
    assert completed["trace.id"] == sent["trace.id"]
    assert completed["span.id"] == sent["span.id"]
    assert completed["message.input"] == sent["message.input"]
    assert json.loads(completed["message.output"]) == (
        "second" if streaming else "response"
    )
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
