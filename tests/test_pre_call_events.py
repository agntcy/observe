# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import asyncio
import contextlib
import http.server
import json
import threading
from types import SimpleNamespace

import pytest
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider

from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.config import set_realtime_observability_enabled
from ioa_observe.sdk.instrumentations import mcp as mcp_instrumentation
from ioa_observe.sdk.instrumentations.a2a import _emit_a2a_send_topology_event
from ioa_observe.sdk.tracing import (
    clear_runtime_event_listeners,
    pre_call_events,
    register_runtime_event_listener,
    tracing,
)
from ioa_observe.sdk.tracing.topology import clear_topology_listeners

TRACEPARENT = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"


@pytest.fixture(autouse=True)
def realtime_events():
    clear_runtime_event_listeners()
    clear_topology_listeners()
    set_realtime_observability_enabled(True)
    with kv_store._lock:
        kv_store.store.clear()
    events = []
    register_runtime_event_listener(events.append)
    yield events
    clear_runtime_event_listeners()
    clear_topology_listeners()
    set_realtime_observability_enabled(None)
    with kv_store._lock:
        kv_store.store.clear()


def tool_started(events):
    return [e for e in events if e["event.name"] == "tool.started"]


def test_tool_started_before_send_carries_the_input_call_id_and_agent(realtime_events):
    pre_call_events.emit_tool_started_before_send(
        "session-1",
        "add",
        {"b": 2, "a": 1},
        call_id="call-7",
        agent_name="planner",
    )

    [event] = tool_started(realtime_events)
    assert event["tool.name"] == "add"
    assert json.loads(event["tool.input"]) == {"a": 1, "b": 2}
    assert event["tool.call.id"] == "call-7"
    assert event["agent.name"] == "planner"
    assert event["session.id"] == "session-1"


def test_tool_started_before_send_keeps_a_string_input_as_is(realtime_events):
    pre_call_events.emit_tool_started_before_send("session-1", "echo", "plain text")

    [event] = tool_started(realtime_events)
    assert event["tool.input"] == "plain text"


def test_tool_started_before_send_never_breaks_the_request_it_describes(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("exporter down")

    monkeypatch.setattr(pre_call_events, "emit_runtime_event", boom)

    pre_call_events.emit_tool_started_before_send("session-1", "add", {"a": 1})


def a2a_request(text):
    return SimpleNamespace(
        params=SimpleNamespace(
            metadata={
                "observe": {
                    "session_id": "session-123",
                    "traceparent": TRACEPARENT,
                    "last_agent_name": "planner",
                }
            },
            message=SimpleNamespace(
                messageId="msg-123",
                parts=[SimpleNamespace(text=text), {"text": "second line"}],
            ),
        )
    )


def test_a2a_send_emits_the_message_text_with_the_message_id_as_call_id(
    realtime_events,
):
    client = SimpleNamespace(agent_card=SimpleNamespace(name="executor"))

    _emit_a2a_send_topology_event(
        a2a_request("export the data"), client, "send_message"
    )

    [event] = tool_started(realtime_events)
    assert event["tool.name"] == "a2a_message:executor"
    assert json.loads(event["tool.input"]) == {"text": "export the data\nsecond line"}
    assert event["tool.call.id"] == "msg-123"
    assert event["session.id"] == "session-123"
    assert event["agent.name"] == "planner"
    [sent] = [e for e in realtime_events if e["event.name"] == "a2a.message.sent"]
    assert event["event.parent.id"] == sent["event.id"]


def test_a2a_send_emits_the_text_event_after_a2a_message_sent_on_the_same_span(
    realtime_events,
):
    client = SimpleNamespace(agent_card=SimpleNamespace(name="executor"))
    provider = TracerProvider()

    with provider.get_tracer(__name__).start_as_current_span("send") as span:
        _emit_a2a_send_topology_event(a2a_request("hello"), client, "send_message")

    names = [e["event.name"] for e in realtime_events]
    assert names.index("a2a.message.sent") < names.index("tool.started")
    span_id = format(span.get_span_context().span_id, "016x")
    assert {
        e["span.id"]
        for e in realtime_events
        if e["event.name"] in ("a2a.message.sent", "tool.started")
    } == {span_id}


def test_mcp_tool_call_is_emitted_only_when_the_library_injects_its_trace_context(
    realtime_events,
):
    params = {"name": "add", "arguments": {"a": 1, "b": 2}}

    mcp_instrumentation._stage_mcp_tool_call_started(params, "session-1", "req-1")
    assert tool_started(realtime_events) == []

    result = mcp_instrumentation.McpInstrumentor._inject_with_staged_tool_call(
        lambda meta: meta.update(injected=True) or "done", None, ({},), {}
    )

    assert result == "done"
    [event] = tool_started(realtime_events)
    assert event["tool.name"] == "add"
    assert json.loads(event["tool.input"]) == {"a": 1, "b": 2}
    assert event["tool.call.id"] == "req-1"


def test_mcp_tool_call_event_points_to_the_outgoing_sent_event(realtime_events):
    mcp_instrumentation._stage_mcp_tool_call_started(
        {"name": "add", "arguments": {}}, "session-1", "req-1", "sent-event-id"
    )

    mcp_instrumentation.McpInstrumentor._inject_with_staged_tool_call(
        lambda: None, None, (), {}
    )

    [event] = tool_started(realtime_events)
    assert event["event.parent.id"] == "sent-event-id"


def test_mcp_client_wrapper_stages_the_call_with_the_id_of_its_sent_event(
    realtime_events,
):
    pytest.importorskip("mcp.types")
    traceparent = TRACEPARENT
    session_id = "session-123"
    kv_store.set(f"execution.{traceparent}", session_id)

    class Params:
        meta = None

        def model_dump(self, **kwargs):
            return {"name": "add", "arguments": {"a": 1}}

    root = SimpleNamespace(method="tools/call", params=Params(), id="req-1")
    session = SimpleNamespace(
        _write_stream=SimpleNamespace(observe_peer_name="mcp://math-server")
    )

    async def wrapped(*args, **kwargs):
        # The library injects its trace context while the request is sent.
        mcp_instrumentation.McpInstrumentor._inject_with_staged_tool_call(
            lambda: None, None, (), {}
        )
        return SimpleNamespace(isError=False, content=[])

    instrumented = mcp_instrumentation.McpInstrumentor().patch_mcp_client(
        trace.get_tracer(__name__)
    )
    ctx = context.set_value("current_traceparent", traceparent)
    ctx = context.set_value("session.id", session_id, context=ctx)
    token = context.attach(context.set_value("workflow_name", "planner", context=ctx))
    try:
        asyncio.run(instrumented(wrapped, session, (SimpleNamespace(root=root),), {}))
    finally:
        context.detach(token)

    [sent] = [e for e in realtime_events if e["event.name"] == "mcp.message.sent"]
    [event] = tool_started(realtime_events)
    assert event["event.parent.id"] == sent["event.id"]
    assert event["tool.name"] == "add"


def test_mcp_tool_call_is_emitted_once_per_request(realtime_events):
    mcp_instrumentation._stage_mcp_tool_call_started(
        {"name": "add", "arguments": {}}, "session-1", "req-1"
    )
    inject = mcp_instrumentation.McpInstrumentor._inject_with_staged_tool_call

    inject(lambda: None, None, (), {})
    inject(lambda: None, None, (), {})

    assert len(tool_started(realtime_events)) == 1


def test_an_inject_without_a_staged_tool_call_emits_nothing(realtime_events):
    mcp_instrumentation.McpInstrumentor._inject_with_staged_tool_call(
        lambda: None, None, (), {}
    )

    assert tool_started(realtime_events) == []


def test_a_request_without_a_tool_name_is_not_staged(realtime_events):
    mcp_instrumentation._stage_mcp_tool_call_started({"arguments": {}}, "s", "r")
    mcp_instrumentation.McpInstrumentor._inject_with_staged_tool_call(
        lambda: None, None, (), {}
    )

    assert tool_started(realtime_events) == []


def test_the_mcp_library_still_injects_trace_context_where_the_hook_expects_it(
    monkeypatch, realtime_events
):
    """Fails if the MCP library renames or moves ``inject_trace_context``, or stops calling it inside its own
    ``MCP send`` span: the pre-call event of a tool call would silently stop being emitted."""
    dispatcher = pytest.importorskip("mcp.shared.jsonrpc_dispatcher")
    anyio = pytest.importorskip("anyio")
    from mcp.shared.message import SessionMessage
    from wrapt import FunctionWrapper

    tracer = TracerProvider().get_tracer(__name__)
    send_span_ids = []

    @contextlib.contextmanager
    def recording_span(name, **kwargs):
        with tracer.start_as_current_span(name) as span:
            send_span_ids.append(span.get_span_context().span_id)
            yield span

    monkeypatch.setattr(dispatcher, "otel_span", recording_span)
    monkeypatch.setattr(
        dispatcher,
        "inject_trace_context",
        FunctionWrapper(
            dispatcher.inject_trace_context,
            mcp_instrumentation.McpInstrumentor._inject_with_staged_tool_call,
        ),
    )
    emitted_in = []
    emit = pre_call_events.emit_runtime_event
    monkeypatch.setattr(
        pre_call_events,
        "emit_runtime_event",
        lambda attributes, **kw: (
            emitted_in.append(trace.get_current_span().get_span_context().span_id),
            emit(attributes, **kw),
        )[1],
    )

    async def on_request(ctx, method, params):
        return {}

    async def on_notify(ctx, method, params):
        return None

    async def run():
        c2s_send, c2s_recv = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](8)
        s2c_send, s2c_recv = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](8)
        client = dispatcher.JSONRPCDispatcher(s2c_recv, c2s_send)
        server = dispatcher.JSONRPCDispatcher(c2s_recv, s2c_send)
        async with anyio.create_task_group() as tg:
            await tg.start(client.run, on_request, on_notify)
            await tg.start(server.run, on_request, on_notify)
            mcp_instrumentation._stage_mcp_tool_call_started(
                {"name": "add", "arguments": {"a": 1}}, "session-1", "req-1"
            )
            with anyio.fail_after(5):
                await client.send_raw_request("tools/call", {"name": "add"})
            tg.cancel_scope.cancel()

    asyncio.run(run())

    assert len(tool_started(realtime_events)) == 1
    assert emitted_in == send_span_ids


PROMPT = '[{"role":"user","parts":[{"type":"text","content":"hello"}]}]'


@pytest.fixture
def tracer_wrapper(monkeypatch):
    from ioa_observe.sdk import TracerWrapper

    counted = []
    wrapper = SimpleNamespace(
        _active_llm_spans={},
        _active_llm_spans_lock=threading.Lock(),
        llm_call_counter=SimpleNamespace(
            add=lambda n, attributes=None: counted.append(n)
        ),
        _should_capture_content=lambda: True,
    )
    monkeypatch.setattr(TracerWrapper, "instance", wrapper, raising=False)
    return wrapper


def llm_span(**attributes):
    tracer = TracerProvider().get_tracer(__name__)
    span = tracer.start_span("chat gpt", attributes=attributes)
    return span


def llm_started(events):
    return [e for e in events if e["event.name"] == "llm.started"]


def test_llm_started_is_emitted_with_the_prompt_before_the_call_ends(
    realtime_events, tracer_wrapper
):
    span = llm_span(
        **{
            "gen_ai.operation.name": "chat",
            "gen_ai.input.messages": PROMPT,
            "session.id": "session-1",
        }
    )

    tracing.emit_llm_started_for(span)

    [event] = llm_started(realtime_events)
    assert event["llm.input"] == PROMPT
    assert event["session.id"] == "session-1"
    assert event["llm.call.id"] == format(span.get_span_context().span_id, "016x")
    assert span.get_span_context().span_id in tracer_wrapper._active_llm_spans


def test_llm_started_is_emitted_once_per_span(realtime_events, tracer_wrapper):
    span = llm_span(
        **{
            "gen_ai.operation.name": "chat",
            "gen_ai.input.messages": PROMPT,
            "session.id": "session-1",
        }
    )

    tracing.emit_llm_started_for(span)
    tracing.emit_llm_started_for(span)

    assert len(llm_started(realtime_events)) == 1


@pytest.mark.parametrize(
    "attributes",
    [
        {"gen_ai.operation.name": "execute_tool", "session.id": "session-1"},
        {"gen_ai.operation.name": "chat"},
    ],
    ids=["not-an-llm-span", "no-session"],
)
def test_llm_started_is_skipped_for_other_spans(
    realtime_events, tracer_wrapper, attributes
):
    tracing.emit_llm_started_for(llm_span(**attributes))

    assert llm_started(realtime_events) == []
    assert tracer_wrapper._active_llm_spans == {}


def test_langchain_callbacks_emit_llm_started_after_the_original_has_set_the_request(
    monkeypatch, realtime_events, tracer_wrapper
):
    handler = pytest.importorskip(
        "opentelemetry.instrumentation.langchain.callback_handler"
    )
    order = []
    span = llm_span(
        **{
            "gen_ai.operation.name": "chat",
            "gen_ai.input.messages": PROMPT,
            "session.id": "session-1",
        }
    )

    def original(self, serialized, messages, *args, run_id=None, **kwargs):
        order.append("original")
        return "original-result"

    for name in ("on_chat_model_start", "on_llm_start"):
        monkeypatch.setattr(handler.TraceloopCallbackHandler, name, original)
    tracing._emit_llm_started_before_the_call()
    tracing._emit_llm_started_before_the_call()
    holder = SimpleNamespace(spans={"run-1": SimpleNamespace(span=span)})

    result = handler.TraceloopCallbackHandler.on_chat_model_start(
        holder, {}, [], run_id="run-1"
    )

    assert result == "original-result"
    assert order == ["original"]
    assert len(llm_started(realtime_events)) == 1


def test_a_failing_llm_started_does_not_break_the_langchain_callback(
    monkeypatch, realtime_events, tracer_wrapper
):
    handler = pytest.importorskip(
        "opentelemetry.instrumentation.langchain.callback_handler"
    )

    def original(self, serialized, messages, *args, run_id=None, **kwargs):
        return "original-result"

    for name in ("on_chat_model_start", "on_llm_start"):
        monkeypatch.setattr(handler.TraceloopCallbackHandler, name, original)
    tracing._emit_llm_started_before_the_call()
    holder = SimpleNamespace(spans=None)

    result = handler.TraceloopCallbackHandler.on_chat_model_start(
        holder, {}, [], run_id="run-1"
    )

    assert result == "original-result"


def serve_chat_completions(on_request):
    """A local OpenAI-compatible server; ``on_request`` is called when a request arrives, before it is answered."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", 0)))
            on_request()
            body = json.dumps(
                {
                    "id": "x",
                    "object": "chat.completion",
                    "model": "m",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "ok"},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("use_async_client", [True, False], ids=["async", "sync"])
def test_raw_openai_client_emits_llm_started_with_the_prompt_before_the_request_is_sent(
    realtime_events, use_async_client
):
    openai = pytest.importorskip("openai")
    pytest.importorskip("opentelemetry.instrumentation.openai")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from ioa_observe.sdk import Observe
    from ioa_observe.sdk.tracing import session_start

    events_at_the_server = []
    server = serve_chat_completions(
        lambda: events_at_the_server.append([dict(e) for e in realtime_events])
    )
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    messages = [{"role": "user", "content": "Where is the train to Luminos?"}]
    try:
        Observe.init(
            app_name="pre-call-openai",
            exporter=InMemorySpanExporter(),
            realtime_observability_enabled=True,
        )
        with session_start():
            if use_async_client:

                async def call():
                    client = openai.AsyncOpenAI(
                        base_url=base_url, api_key="k", max_retries=0
                    )
                    await client.chat.completions.create(model="m", messages=messages)

                asyncio.run(call())
            else:
                client = openai.OpenAI(base_url=base_url, api_key="k", max_retries=0)
                client.chat.completions.create(model="m", messages=messages)
    finally:
        server.shutdown()

    [at_arrival] = events_at_the_server
    started_before = [e for e in at_arrival if e["event.name"] == "llm.started"]
    assert len(started_before) == 1
    assert "Where is the train to Luminos?" in started_before[0]["llm.input"]
    # Span processors left over from other tests in the same process each add their own events at the end of
    # the span, so only the order for this call is asserted, not how many times each event appears.
    call_id = started_before[0]["llm.call.id"]
    names = [
        e["event.name"] for e in realtime_events if e.get("llm.call.id") == call_id
    ]
    assert names.index("llm.started") < names.index("llm.completed")


def test_mcp_list_request_is_told_apart_from_a_tool_call(realtime_events):
    """``tools/list`` emits ``mcp.message.sent`` with its method in ``operation.name`` and no ``tool.started``."""
    pytest.importorskip("mcp.types")
    session_id = "session-123"
    kv_store.set(f"execution.{TRACEPARENT}", session_id)

    class Params:
        meta = None

        def model_dump(self, **kwargs):
            return {}

    root = SimpleNamespace(method="tools/list", params=Params(), id="req-2")
    session = SimpleNamespace(
        _write_stream=SimpleNamespace(observe_peer_name="mcp://math-server")
    )

    async def wrapped(*args, **kwargs):
        mcp_instrumentation.McpInstrumentor._inject_with_staged_tool_call(
            lambda: None, None, (), {}
        )
        return SimpleNamespace(isError=False, tools=[])

    instrumented = mcp_instrumentation.McpInstrumentor().patch_mcp_client(
        trace.get_tracer(__name__)
    )
    ctx = context.set_value("current_traceparent", TRACEPARENT)
    ctx = context.set_value("session.id", session_id, context=ctx)
    token = context.attach(context.set_value("workflow_name", "planner", context=ctx))
    try:
        asyncio.run(instrumented(wrapped, session, (SimpleNamespace(root=root),), {}))
    finally:
        context.detach(token)

    [sent] = [e for e in realtime_events if e["event.name"] == "mcp.message.sent"]
    assert sent["operation.name"] == "tools/list"
    assert tool_started(realtime_events) == []
