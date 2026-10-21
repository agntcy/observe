# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
from types import SimpleNamespace

import pytest
from opentelemetry import baggage
from opentelemetry.trace import get_current_span, get_tracer
from opentelemetry.context import Context, attach, detach, get_value, set_value
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from ioa_observe.sdk import TracerWrapper
from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.instrumentations.mcp import (
    McpInstrumentor,
    InstrumentedStreamReader,
    InstrumentedStreamWriter,
    _PENDING_TOOL_CALL,
)
from ioa_observe.sdk.tracing import (
    RuntimeEventAttribute,
    RuntimeEventName,
    clear_runtime_event_listeners,
    get_live_topology_snapshot,
    register_runtime_event_listener,
    unregister_runtime_event_listener,
)
from ioa_observe.sdk.tracing.topology import clear_topology_listeners


@pytest.fixture(autouse=True)
def reset_mcp_realtime_state(monkeypatch):
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


def test_mcp_initialization_identifies_provider_and_generates_request_correlation():
    types = pytest.importorskip("mcp.types")
    from ioa_observe.sdk.instrumentations.mcp import _emit_mcp_receive_topology_event

    events = []
    register_runtime_event_listener(events.append)
    session = SimpleNamespace(_write_stream=SimpleNamespace(name="mcp"))
    provider = TracerProvider()
    patched = McpInstrumentor().patch_mcp_client(provider.get_tracer(__name__))

    async def initialized(request):
        return SimpleNamespace(
            serverInfo=SimpleNamespace(name="travel-tools", version="1")
        )

    async def listed(request):
        return types.ListToolsResult(tools=[])

    async def run():
        token = attach(set_value("session.id", "identity-session"))
        try:
            await patched(
                initialized,
                session,
                (
                    SimpleNamespace(
                        root=SimpleNamespace(method="initialize", params={})
                    ),
                ),
                {},
            )
            request = types.ClientRequest(types.ListToolsRequest())
            for _ in range(2):
                await patched(listed, session, (request,), {})
                meta = request.root.params.meta
                if hasattr(meta, "model_dump"):
                    meta = meta.model_dump(by_alias=True, exclude_none=True)
                _emit_mcp_receive_topology_event(meta, "tools/list", message_id="2")
        finally:
            detach(token)

    asyncio.run(run())
    [initialized_event] = [
        event
        for event in events
        if event["event.name"] == "mcp.message.sent"
        and event["operation.name"] == "initialize"
    ]
    assert initialized_event["target.agent"] == "mcp"
    listed_events = [
        event
        for event in events
        if event["event.name"] == "mcp.message.sent"
        and event["operation.name"] == "tools/list"
    ]
    assert [event["target.agent"] for event in listed_events] == ["travel-tools"] * 2
    assert len({event["message.id"] for event in listed_events}) == 2
    received = [
        event for event in events if event["event.name"] == "mcp.message.received"
    ]
    assert [event["message.id"] for event in received] == [
        event["message.id"] for event in listed_events
    ]
    discovered = [
        event for event in events if event["event.name"] == "mcp.tools.discovered"
    ]
    assert [event["capability.provider.id"] for event in discovered] == [
        "travel-tools"
    ] * 2


@pytest.mark.parametrize("transport_identity", [True, False])
def test_mcp_transport_identity_takes_priority_over_initialized_name(
    transport_identity,
):
    from ioa_observe.sdk.instrumentations.mcp import _extract_mcp_peer_name

    endpoint = "https://travel-tools.test/mcp"
    session = SimpleNamespace(_observe_server_info={"name": "travel-tools"})
    if transport_identity:
        session._write_stream = SimpleNamespace(observe_peer_name=endpoint)
    else:
        session.url = endpoint
    assert _extract_mcp_peer_name(session) == endpoint


@pytest.mark.parametrize("nested", [True, False])
def test_mcp_pending_tool_state_is_restored_after_request(nested):
    pytest.importorskip("mcp")
    events = []
    register_runtime_event_listener(events.append)
    session = SimpleNamespace(_write_stream=SimpleNamespace(observe_peer_name="tools"))
    provider = TracerProvider()
    wrapper = McpInstrumentor().patch_mcp_client(provider.get_tracer(__name__))
    original_pending = {"outer": "preserved"}

    def request(call_id):
        return SimpleNamespace(
            root=SimpleNamespace(
                method="tools/call",
                id=call_id,
                params={"name": call_id, "arguments": {}},
            )
        )

    async def nested_request(request):
        McpInstrumentor._inject_with_staged_tool_call(lambda: None, None, (), {})
        return SimpleNamespace(isError=False, content=[])

    async def outer_request(_request):
        outer_pending = _PENDING_TOOL_CALL.get()
        if nested:
            await wrapper(nested_request, session, (request("inner"),), {})
            assert _PENDING_TOOL_CALL.get() is outer_pending
            McpInstrumentor._inject_with_staged_tool_call(lambda: None, None, (), {})
        return SimpleNamespace(isError=False, content=[])

    async def invoke():
        session_token = attach(set_value("session.id", "session"))
        pending_token = _PENDING_TOOL_CALL.set(original_pending)
        try:
            await wrapper(outer_request, session, (request("outer"),), {})
            assert _PENDING_TOOL_CALL.get() is original_pending
        finally:
            _PENDING_TOOL_CALL.reset(pending_token)
            detach(session_token)

    try:
        asyncio.run(invoke())
    finally:
        provider.shutdown()
    started = [event for event in events if event["event.name"] == "tool.started"]
    completed = [event for event in events if event["event.name"] == "tool.completed"]
    from importlib.util import find_spec

    expected = 2 if nested else int(find_spec("mcp.shared.jsonrpc_dispatcher") is None)
    assert len(started) == len(completed) == expected
    assert {event["tool.call.id"] for event in completed} == {
        event["tool.call.id"] for event in started
    }


def test_patch_mcp_client_injects_realtime_protocol_metadata_and_events():
    pytest.importorskip("mcp")
    runtime_events = []
    register_runtime_event_listener(runtime_events.append)

    class Params:
        def __init__(self):
            self.meta = None

    class Root:
        def __init__(self):
            self.method = "tools/call"
            self.params = Params()
            self.id = "req-1"

    request = SimpleNamespace(root=Root())
    session = SimpleNamespace(
        _write_stream=SimpleNamespace(observe_peer_name="mcp://math-server")
    )

    traceparent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    session_id = "session-123"
    kv_store.set(f"execution.{traceparent}", session_id)
    kv_store.set(f"session.{session_id}.last_agent_name", "planner")
    kv_store.set(f"session.{session_id}.last_agent_span_id", "0000000000000123")
    kv_store.set(
        f"session.{session_id}.last_agent_trace_id",
        "00000000000000000000000000000456",
    )
    kv_store.set(f"session.{session_id}.agent_sequence", "2")
    kv_store.set(f"session.{session_id}.agents.2.fork_id", "fork-1")

    async def wrapped(*args, **kwargs):
        return SimpleNamespace(isError=False, content=[])

    instrumented = McpInstrumentor().patch_mcp_client(get_tracer(__name__))

    ctx = set_value("current_traceparent", traceparent)
    ctx = set_value("session.id", session_id, context=ctx)
    token = attach(set_value("workflow_name", "planner", context=ctx))
    try:
        result = asyncio.run(instrumented(wrapped, session, (request,), {}))
    finally:
        detach(token)
        unregister_runtime_event_listener(runtime_events.append)

    assert result.isError is False
    assert request.root.params.meta["session.id"] == session_id
    assert request.root.params.meta["traceparent"] != traceparent
    assert request.root.params.meta["source_agent"] == "planner"
    assert request.root.params.meta["target_agent"] == "mcp://math-server"

    sent = next(
        event
        for event in runtime_events
        if event[RuntimeEventAttribute.EVENT_NAME.value]
        == RuntimeEventName.MCP_MESSAGE_SENT.value
    )
    assert sent[RuntimeEventAttribute.SOURCE_AGENT.value] == "planner"
    assert sent[RuntimeEventAttribute.TARGET_AGENT.value] == "mcp://math-server"
    assert sent[RuntimeEventAttribute.MESSAGE_ID.value] == "req-1"
    assert sent["operation.name"] == "tools/call"
    assert request.root.params.meta["event.id"] == sent["event.id"]
    assert sent[RuntimeEventAttribute.SEQUENCE.value] == 2
    assert sent[RuntimeEventAttribute.FORK_ID.value] == "fork-1"
    _, trace_id, span_id, _ = request.root.params.meta["traceparent"].split("-")
    assert sent["trace.id"] == trace_id
    assert sent["span.id"] == span_id
    assert (
        sent["source.agent.span.id"] == request.root.params.meta["last_agent_span_id"]
    )
    assert (
        sent["source.agent.trace.id"] == request.root.params.meta["last_agent_trace_id"]
    )
    assert sent["source.agent.span.id"] != sent["span.id"]
    [completed] = [
        event
        for event in runtime_events
        if event["event.name"] == "mcp.message.completed"
    ]
    assert completed["event.parent.id"] == sent["event.id"]
    assert completed["trace.id"] == sent["trace.id"]
    assert completed["span.id"] == sent["span.id"]
    assert completed["message.input"] == sent["message.input"]
    assert json.loads(completed["message.output"]) == {
        "isError": False,
        "content": [],
    }

    snapshot = get_live_topology_snapshot(session_id)
    assert snapshot["edges"][0]["id"] == "mcp:planner->mcp://math-server"
    assert snapshot["edges"][0]["status"] == "sent"


@pytest.mark.parametrize("capture_content", [True, False])
@pytest.mark.parametrize("outcome", ["success", "tool_error", "exception", "cancelled"])
def test_mcp_message_payloads_and_completion(monkeypatch, capture_content, outcome):
    types = pytest.importorskip("mcp.types")
    monkeypatch.setenv("OBSERVE_TRACE_CONTENT", str(capture_content).lower())
    events = []
    register_runtime_event_listener(events.append)
    request = types.JSONRPCMessage(
        types.JSONRPCRequest(
            method="tools/call",
            jsonrpc="2.0",
            id="req-payload",
            params={
                "name": "lookup",
                "arguments": {"query": "hello", "filters": {"limit": 2}},
                "_meta": {"custom": "kept", "progressToken": "progress-1"},
            },
        )
    )
    result = types.CallToolResult(
        isError=outcome == "tool_error",
        content=[types.TextContent(type="text", text="result")],
    )
    session = SimpleNamespace(
        _write_stream=SimpleNamespace(observe_peer_name="mcp://lookup")
    )

    async def wrapped(argument):
        assert argument is request
        McpInstrumentor._inject_with_staged_tool_call(lambda: None, None, (), {})
        if outcome == "exception":
            raise RuntimeError("remote failed")
        if outcome == "cancelled":
            raise asyncio.CancelledError()
        return result

    provider = TracerProvider()
    patched = McpInstrumentor().patch_mcp_client(provider.get_tracer(__name__))
    token = attach(set_value("session.id", "payload-session"))
    try:
        if outcome in ("exception", "cancelled"):
            error = RuntimeError if outcome == "exception" else asyncio.CancelledError
            with pytest.raises(error):
                asyncio.run(patched(wrapped, session, (request,), {}))
        else:
            assert asyncio.run(patched(wrapped, session, (request,), {})) is result
    finally:
        detach(token)
        provider.shutdown()

    [sent] = [event for event in events if event["event.name"] == "mcp.message.sent"]
    completed = [
        event for event in events if event["event.name"] == "mcp.message.completed"
    ]
    assert len(completed) == (0 if outcome in ("exception", "cancelled") else 1)
    params = request.root.params
    assert params["_meta"]["custom"] == "kept"
    assert params["_meta"]["event.id"] == sent["event.id"]
    for event in [sent, *completed]:
        assert ("message.input" in event) is capture_content
        if capture_content:
            assert json.loads(event["message.input"]) == {
                "name": "lookup",
                "arguments": {"query": "hello", "filters": {"limit": 2}},
            }
        else:
            assert "message.output" not in event
    if completed:
        assert completed[0]["event.parent.id"] == sent["event.id"]
        assert completed[0]["message.id"] == sent["message.id"] == "req-payload"
        assert completed[0]["trace.id"] == sent["trace.id"]
        assert completed[0]["span.id"] == sent["span.id"]
        if capture_content:
            assert json.loads(completed[0]["message.output"]) == result.model_dump(
                mode="json", by_alias=True
            )
    tool_events = [event for event in events if event["event.name"] == "tool.started"]
    assert len(tool_events) == (1 if capture_content else 0)
    tool_completions = [
        event for event in events if event["event.name"] == "tool.completed"
    ]
    assert len(tool_completions) == len(tool_events)
    if tool_events:
        assert (
            tool_completions[0]["tool.call.id"]
            == tool_events[0]["tool.call.id"]
            == "req-payload"
        )
        assert tool_completions[0]["span.id"] == tool_events[0]["span.id"]
        assert tool_completions[0]["tool.status"] == (
            "success" if outcome == "success" else "error"
        )


@pytest.mark.parametrize("capture_content", [True, False])
def test_mcp_native_server_receive_payload(monkeypatch, capture_content):
    monkeypatch.setenv("OBSERVE_TRACE_CONTENT", str(capture_content).lower())
    events = []
    register_runtime_event_listener(events.append)
    request_context = SimpleNamespace(
        method="tools/call",
        request_id="req-1",
        params={"name": "lookup", "arguments": {"x": 3}},
        meta={
            "session.id": "server-session",
            "source_agent": "planner",
            "target_agent": "mcp://server",
            "event.id": "sent-event",
        },
    )

    async def call_next(ctx):
        assert ctx is request_context
        assert get_value("session.id") == "server-session"
        return "response"

    async def middleware(ctx, handler):
        return await handler(ctx)

    token = attach(Context())
    try:
        result = asyncio.run(
            McpInstrumentor._server_context_wrapper(
                middleware, None, (request_context, call_next), {}
            )
        )
        assert result == "response"
        assert get_value("session.id") is None
    finally:
        detach(token)
    [received] = [
        event for event in events if event["event.name"] == "mcp.message.received"
    ]
    assert received["event.parent.id"] == "sent-event"
    assert ("message.input" in received) is capture_content
    if capture_content:
        assert json.loads(received["message.input"]) == request_context.params
    assert "message.output" not in received


def test_mcp_disabled_content_clears_staged_tool_input(monkeypatch):
    from ioa_observe.sdk.instrumentations.mcp import _stage_mcp_tool_call_started

    token = _PENDING_TOOL_CALL.set(("stale input",))
    monkeypatch.setenv("OBSERVE_TRACE_CONTENT", "false")
    try:
        _stage_mcp_tool_call_started({"name": "lookup"}, "session", "req")
        assert _PENDING_TOOL_CALL.get() is None
    finally:
        _PENDING_TOOL_CALL.reset(token)


@pytest.mark.parametrize("invalid_payload", ["input", "output"])
def test_mcp_payload_serialization_failure_preserves_call(
    monkeypatch, caplog, invalid_payload
):
    pytest.importorskip("mcp.types")
    monkeypatch.setenv("OBSERVE_TRACE_CONTENT", "true")
    events = []
    register_runtime_event_listener(events.append)
    request = {
        "method": "ping",
        "id": "req-1",
        "params": {"value": object() if invalid_payload == "input" else "input"},
    }
    result = SimpleNamespace(
        isError=False,
        content=[],
        value=object() if invalid_payload == "output" else "output",
    )
    session = SimpleNamespace(
        _write_stream=SimpleNamespace(observe_peer_name="mcp://server")
    )

    async def wrapped(*, request):
        return result

    provider = TracerProvider()
    patched = McpInstrumentor().patch_mcp_client(provider.get_tracer(__name__))
    token = attach(set_value("session.id", "serialization-session"))
    try:
        assert (
            asyncio.run(patched(wrapped, session, (), {"request": request})) is result
        )
    finally:
        detach(token)
        provider.shutdown()
    [sent] = [event for event in events if event["event.name"] == "mcp.message.sent"]
    [completed] = [
        event for event in events if event["event.name"] == "mcp.message.completed"
    ]
    assert completed["event.parent.id"] == sent["event.id"]
    assert f"message.{invalid_payload}" not in completed
    assert "Unable to serialize MCP message payload" in caplog.text


@pytest.mark.parametrize("with_params", [True, False])
def test_patch_mcp_client_preserves_list_tools_metadata(with_params):
    types = pytest.importorskip("mcp.types")
    request = types.ListToolsRequest(
        params=(
            types.PaginatedRequestParams(
                cursor="next-page",
                _meta={"progressToken": "progress-1", "custom": {"kept": True}},
            )
            if with_params
            else None
        )
    )
    sent = []
    result = types.ListToolsResult(tools=[])

    async def wrapped(argument):
        sent.append(argument.model_dump(by_alias=True, exclude_none=True))
        return result

    provider = TracerProvider()
    patched = McpInstrumentor().patch_mcp_client(provider.get_tracer(__name__))
    token = attach(set_value("session.id", "list-tools-session"))
    try:
        assert (
            asyncio.run(patched(wrapped, SimpleNamespace(), (request,), {})) is result
        )
    finally:
        detach(token)
        provider.shutdown()

    assert len(sent) == 1
    assert sent[0]["method"] == "tools/list"
    meta = sent[0]["params"]["_meta"]
    assert meta["session.id"] == "list-tools-session"
    assert meta["traceparent"]
    if with_params:
        assert sent[0]["params"]["cursor"] == "next-page"
        assert meta["progressToken"] == "progress-1"
        assert meta["custom"] == {"kept": True}


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_mcp_http_tool_lifecycle_preserves_request_session_and_trace(
    mode,
    monkeypatch,
):
    pytest.importorskip("mcp.server.mcpserver")
    import socket

    import uvicorn
    from mcp import Client
    from mcp.server import MCPServer
    from mcp.server._otel import OpenTelemetryMiddleware
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import (
        InMemoryLogRecordExporter,
        SimpleLogRecordProcessor,
    )

    from ioa_observe.sdk.decorators import tool

    events = []
    register_runtime_event_listener(events.append)
    logger_provider = LoggerProvider()
    log_exporter = InMemoryLogRecordExporter()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    monkeypatch.setattr(
        "ioa_observe.sdk.tracing.runtime_event_emitter._ensure_runtime_event_logger",
        lambda: None,
    )
    monkeypatch.setattr(
        "opentelemetry._logs.get_logger",
        lambda *args, **kwargs: logger_provider.get_logger(
            "ioa_observe.runtime_events"
        ),
    )
    server = MCPServer("math")

    @server.tool()
    @tool(name="add numbers")
    def add(a: int, b: int) -> int:
        return a + b

    @server.tool()
    @tool(name="failing tool")
    async def fail() -> int:
        raise ValueError("tool failed")

    async def run():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]
            service = uvicorn.Server(
                uvicorn.Config(server.streamable_http_app(), log_level="error")
            )
            task = asyncio.create_task(service.serve(sockets=[sock]))
            try:
                while not service.started:
                    if task.done():
                        await task
                        raise RuntimeError("MCP test server stopped before startup")
                    await asyncio.sleep(0.01)
                async with Client(f"http://127.0.0.1:{port}/mcp", mode=mode) as client:
                    token = attach(set_value("session.id", "math-session"))
                    try:
                        result = await client.call_tool("add", {"a": 3, "b": 5})
                        assert result.structured_content["result"] == 8
                    finally:
                        detach(token)
                    token = attach(set_value("session.id", "failure-session"))
                    try:
                        result = await client.call_tool("fail", {})
                        assert result.is_error
                    finally:
                        detach(token)
                    result = await client.call_tool("add", {"a": 8, "b": 12})
                    assert result.structured_content["result"] == 20
            finally:
                service.should_exit = True
                await task

    instrumentor = McpInstrumentor()
    original_middleware = OpenTelemetryMiddleware.__call__
    instrumentor._instrument()
    token = attach(Context())
    try:
        asyncio.run(asyncio.wait_for(run(), timeout=15))
    finally:
        detach(token)
        instrumentor._uninstrument()
        logger_provider.shutdown()
    assert OpenTelemetryMiddleware.__call__ is original_middleware

    all_tool_events = [
        event for event in events if event["event.name"].startswith("tool.")
    ]
    tool_events = [
        event
        for event in all_tool_events
        if event.get("tool.execution.kind") != "client"
    ]
    assert [event["event.name"] for event in tool_events] == [
        "tool.started",
        "tool.completed",
        "tool.started",
        "tool.completed",
    ]
    for session_id, tool_name, status in (
        ("math-session", "add numbers", "success"),
        ("failure-session", "failing tool", "error"),
    ):
        started, completed = [
            event for event in tool_events if event["session.id"] == session_id
        ]
        sent = next(
            event
            for event in events
            if event["event.name"] == "mcp.message.sent"
            and event["operation.name"] == "tools/call"
            and event["session.id"] == session_id
        )
        received = [
            event
            for event in events
            if event["event.name"] == "mcp.message.received"
            and event["operation.name"] == "tools/call"
            and event["session.id"] == session_id
        ]
        assert len(received) == 1
        assert started["tool.name"] == completed["tool.name"] == tool_name
        assert completed["tool.status"] == status
        assert started["trace.id"] == completed["trace.id"] == sent["trace.id"]
        assert received[0]["trace.id"] == sent["trace.id"]
        assert started["span.id"] == completed["span.id"]
    tool_logs = [
        item.log_record
        for item in log_exporter.get_finished_logs()
        if item.log_record.event_name.startswith("tool.")
    ]
    assert [record.event_name for record in tool_logs] == [
        event["event.name"] for event in all_tool_events
    ]
    for record, event in zip(tool_logs, all_tool_events):
        assert record.attributes["session.id"] == event["session.id"]
        assert record.attributes["tool.name"] == event["tool.name"]
        assert format(record.trace_id, "032x") == event["trace.id"]
        assert format(record.span_id, "016x") == event["span.id"]


@pytest.mark.parametrize("capture_content", [True, False])
def test_mcp_stream_reader_restores_tool_session_without_leaking_context(
    monkeypatch, capture_content
):
    types = pytest.importorskip("mcp.types")
    from ioa_observe.sdk.decorators import tool

    monkeypatch.setenv("OBSERVE_TRACE_CONTENT", str(capture_content).lower())
    events = []
    register_runtime_event_listener(events.append)
    traceparent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    meta = {
        "traceparent": traceparent,
        "session.id": "stream-session",
        "last_agent_name": "planner",
        "agent_sequence": "2",
        "fork_id": "fork-one",
        "fork_parent_seq": "1",
        "fork_branch_index": "1",
    }
    request = types.JSONRPCRequest(
        method="tools/call",
        jsonrpc="2.0",
        id=1,
        params={"name": "stream tool", "arguments": {"x": 1}, "_meta": meta},
    )

    class Stream:
        __slots__ = ()

        async def __aiter__(self):
            yield request

    @tool(name="stream tool")
    def run_tool():
        assert get_value("session.id") == "stream-session"
        assert get_value("current_traceparent") == traceparent
        return "done"

    async def run():
        reader = InstrumentedStreamReader(Stream(), get_tracer(__name__))
        async for item in reader:
            assert item is request
            assert run_tool() == "done"
        assert get_value("session.id") is None
        assert get_value("current_traceparent") is None

    token = attach(Context())
    try:
        asyncio.run(run())
    finally:
        detach(token)
    tool_events = [event for event in events if event["event.name"].startswith("tool.")]
    assert [event["event.name"] for event in tool_events] == [
        "tool.started",
        "tool.completed",
    ]
    assert all(event["session.id"] == "stream-session" for event in tool_events)
    assert all(event["trace.id"] == traceparent.split("-")[1] for event in tool_events)
    assert kv_store.get("session.stream-session.last_agent_name") == "planner"
    assert kv_store.get("session.stream-session.agents.2.fork_id") == "fork-one"
    assert kv_store.get("session.stream-session.agents.2.parent_seq") == "1"
    assert kv_store.get("session.stream-session.agents.2.branch_index") == "1"
    [received] = [
        event for event in events if event["event.name"] == "mcp.message.received"
    ]
    assert ("message.input" in received) is capture_content
    if capture_content:
        assert json.loads(received["message.input"]) == {
            "name": "stream tool",
            "arguments": {"x": 1},
        }


@pytest.mark.parametrize(
    "shape", ["typed", "ping", "jsonrpc", "empty", "missing", "dict"]
)
@pytest.mark.parametrize("sampled", [True, False])
@pytest.mark.parametrize("legacy_session", [True, False])
def test_mcp_sdk_metadata_shapes_and_writer_preserve_request_span(
    shape, sampled, legacy_session
):
    types = pytest.importorskip("mcp.types")
    from mcp.shared.message import SessionMessage

    existing = {
        "progressToken": "progress-1",
        "custom": {"preserved": True},
        "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        "tracestate": "vendor=kept",
        "baggage": "existing=kept",
    }
    incoming = existing["traceparent"]
    if shape == "typed":
        request = types.ClientRequest(
            types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name="math", arguments={"x": 2}, _meta=existing
                )
            )
        )
    elif shape == "ping":
        request = types.ClientRequest(types.PingRequest())
    elif shape == "dict":
        request = {"method": "ping", "params": {"other": "kept", "_meta": existing}}
    else:
        params = {"other": "kept", "_meta": existing} if shape == "jsonrpc" else None
        if shape == "empty":
            params = {}
        request = types.JSONRPCMessage(
            types.JSONRPCRequest(
                method="ping", jsonrpc="2.0", id="req-1", params=params
            )
        )

    events, wire = [], []
    register_runtime_event_listener(events.append)
    provider = TracerProvider(sampler=ALWAYS_ON if sampled else ALWAYS_OFF)
    tracer = provider.get_tracer(__name__)

    class Sink:
        async def send(self, item):
            wire.append(item.message.root.model_dump(by_alias=True, exclude_none=True))
            return "delivered"

    writer = InstrumentedStreamWriter(Sink(), tracer, peer_name="mcp://receiver")
    session = SimpleNamespace(_write_stream=writer)

    async def wrapped(*args, **kwargs):
        argument = args[0] if args else kwargs["request"]
        if hasattr(argument, "model_dump"):
            data = argument.model_dump(by_alias=True, exclude_none=True)
        else:
            data = argument
        if "id" not in data:
            data = {**data, "jsonrpc": "2.0", "id": 1}
        wire_request = types.JSONRPCMessage(types.JSONRPCRequest(**data))
        await writer.send(SessionMessage(wire_request))
        return SimpleNamespace(isError=False, content=[])

    ctx = TraceContextTextMapPropagator().extract({"traceparent": incoming})
    ctx = set_value("current_traceparent", incoming, context=ctx)
    kv_store.set(f"execution.{incoming}", "legacy-session")
    if not legacy_session:
        ctx = set_value("session.id", "current-session", context=ctx)
    ctx = baggage.set_baggage("local", "added", context=ctx)
    token = attach(ctx)
    try:
        patched = McpInstrumentor().patch_mcp_client(tracer)
        # Exercise the keyword-only request path as well as positional requests.
        args, kwargs = (
            ((), {"request": request}) if shape == "ping" else ((request,), {})
        )
        asyncio.run(patched(wrapped, session, args, kwargs))
    finally:
        detach(token)
        provider.shutdown()

    meta = wire[0]["params"]["_meta"]
    sent = next(event for event in events if event["event.name"] == "mcp.message.sent")
    [completed] = [
        event for event in events if event["event.name"] == "mcp.message.completed"
    ]
    assert completed["event.parent.id"] == sent["event.id"]
    assert completed["trace.id"] == sent["trace.id"]
    assert completed["span.id"] == sent["span.id"]
    assert completed["message.input"] == sent["message.input"]
    if shape == "typed":
        assert json.loads(sent["message.input"])["arguments"] == {"x": 2}
    assert "_meta" not in json.loads(sent["message.input"])
    _, trace_id, span_id, flags = meta["traceparent"].split("-")
    assert meta["traceparent"] != incoming
    assert sent["trace.id"] == trace_id
    assert sent["span.id"] == span_id
    assert trace_id == incoming.split("-")[1]
    assert span_id != incoming.split("-")[2]
    assert bool(int(flags, 16) & 1) is sampled
    expected_session = "legacy-session" if legacy_session else "current-session"
    assert meta["session.id"] == expected_session
    assert kv_store.get(f"execution.{meta['traceparent']}") == meta["session.id"]
    assert "local=added" in meta["baggage"]
    if shape in ("typed", "jsonrpc", "dict"):
        assert meta["custom"] == {"preserved": True}
        assert meta["progressToken"] == "progress-1"
        assert meta["tracestate"] == "vendor=kept"
        assert "existing=kept" in meta["baggage"]
    if shape == "typed":
        assert wire[0]["params"]["arguments"] == {"x": 2}
    if shape in ("jsonrpc", "dict"):
        assert wire[0]["params"]["other"] == "kept"
    if shape in ("typed", "ping", "dict"):
        assert sent["message.id"] == meta["observe.message.id"]
        assert len(sent["message.id"]) == 32
    else:
        assert sent["message.id"] == "req-1"
    assert not any(key.startswith("source.") for key in meta)


@pytest.mark.parametrize("with_params", [True, False])
@pytest.mark.parametrize("tool_call", [True, False])
def test_mcp_actual_base_session_serializes_meta_and_progress_tokens(
    with_params, tool_call
):
    types = pytest.importorskip("mcp.types")
    import anyio
    from mcp.client.session import ClientSession

    events, wire = [], []
    register_runtime_event_listener(events.append)
    provider = TracerProvider()
    tracer = provider.get_tracer(__name__)
    incoming = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"

    async def run():
        class Sink:
            async def send(self, item):
                request = item.message.root
                wire.append(request.model_dump(by_alias=True, exclude_none=True))
                if tool_call:
                    assert (
                        len([e for e in events if e["event.name"] == "tool.started"])
                        == 1
                    )
                await session._response_streams[request.id].send(
                    types.JSONRPCResponse(
                        jsonrpc="2.0",
                        id=request.id,
                        result={"isError": False, "content": []} if tool_call else {},
                    )
                )

        read_writer, read_stream = anyio.create_memory_object_stream(1)
        session = ClientSession(
            read_stream,
            InstrumentedStreamWriter(Sink(), tracer, peer_name="mcp://receiver"),
        )
        request = types.ClientRequest(
            types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name="add",
                    arguments={"a": 1},
                    _meta={"custom": "kept"} if with_params else None,
                )
            )
            if tool_call
            else types.PingRequest(
                params=(
                    types.RequestParams(_meta={"custom": "kept"})
                    if with_params
                    else None
                )
            )
        )

        async def progress(*args):
            pass

        patched = McpInstrumentor().patch_mcp_client(tracer)
        result_type = types.CallToolResult if tool_call else types.EmptyResult
        result = await patched(
            session.send_request,
            session,
            (request, result_type),
            {"progress_callback": progress},
        )
        assert isinstance(result, result_type)
        await read_writer.aclose()
        await read_stream.aclose()

    token = attach(
        set_value(
            "session.id",
            "session",
            context=set_value("current_traceparent", incoming),
        )
    )
    try:
        asyncio.run(run())
    finally:
        detach(token)
        provider.shutdown()
    sent = next(event for event in events if event["event.name"] == "mcp.message.sent")
    meta = wire[0]["params"]["_meta"]
    assert meta["progressToken"] == wire[0]["id"]
    assert meta["session.id"] == "session"
    assert meta["observe.message.id"] == sent["message.id"]
    if with_params:
        assert meta["custom"] == "kept"
    assert meta["traceparent"].split("-")[1:3] == [sent["trace.id"], sent["span.id"]]
    if tool_call:
        [started] = [e for e in events if e["event.name"] == "tool.started"]
        [completed] = [e for e in events if e["event.name"] == "tool.completed"]
        assert (
            started["tool.call.id"] == completed["tool.call.id"] == sent["message.id"]
        )
        assert started["span.id"] == completed["span.id"] == sent["span.id"]
        assert completed["event.parent.id"] == started["event.id"]
        assert completed["tool.status"] == "success"


def test_mcp_stream_writer_does_not_change_responses_or_notifications():
    types = pytest.importorskip("mcp.types")
    from mcp.shared.message import SessionMessage

    items = [
        SessionMessage(
            types.JSONRPCMessage(types.JSONRPCResponse(jsonrpc="2.0", id=1, result={}))
        ),
        SessionMessage(
            types.JSONRPCMessage(
                types.JSONRPCNotification(jsonrpc="2.0", method="notifications/test")
            )
        ),
    ]
    delivered = []

    class Sink:
        async def send(self, item):
            delivered.append(item)
            return "delivered"

    async def run():
        writer = InstrumentedStreamWriter(Sink(), get_tracer(__name__))
        for item in items:
            assert await writer.send(item) == "delivered"

    asyncio.run(run())
    assert delivered == items
    assert all(getattr(item.message.root, "params", None) is None for item in items)


@pytest.mark.parametrize("existing_context", [True, False])
def test_mcp_standalone_stream_writer_keeps_preinjected_context_or_creates_one(
    existing_context,
):
    types = pytest.importorskip("mcp.types")
    from mcp.shared.message import SessionMessage

    incoming = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    meta = {"traceparent": incoming, "custom": "kept"} if existing_context else None
    request = types.JSONRPCRequest(
        jsonrpc="2.0", id=1, method="ping", params={"_meta": meta}
    )
    delivered = []
    provider = TracerProvider()

    class Sink:
        async def send(self, item):
            delivered.append(item)

    async def run():
        await InstrumentedStreamWriter(Sink(), provider.get_tracer(__name__)).send(
            SessionMessage(types.JSONRPCMessage(request))
        )

    try:
        asyncio.run(run())
    finally:
        provider.shutdown()
    assert len(delivered) == 1
    wire_meta = request.params["_meta"]
    assert (
        get_current_span(TraceContextTextMapPropagator().extract(wire_meta))
        .get_span_context()
        .is_valid
    )
    if existing_context:
        assert wire_meta == {"traceparent": incoming, "custom": "kept"}
    else:
        assert wire_meta["traceparent"] != incoming


@pytest.mark.parametrize("native_client_context", [True, False])
def test_mcp_uninstrument_restores_actual_base_session_send_request(
    monkeypatch, native_client_context
):
    pytest.importorskip("mcp.shared.session")
    import sys
    from importlib.machinery import ModuleSpec
    from types import ModuleType

    from mcp.shared.session import BaseSession
    from ioa_observe.sdk.instrumentations import mcp as instrumentation

    dispatcher = ModuleType("mcp.shared.jsonrpc_dispatcher")
    dispatcher.__spec__ = ModuleSpec(dispatcher.__name__, loader=None)
    dispatcher.inject_trace_context = lambda: None
    original_inject = dispatcher.inject_trace_context
    if native_client_context:
        import mcp.shared

        monkeypatch.setitem(sys.modules, dispatcher.__name__, dispatcher)
        monkeypatch.setattr(mcp.shared, "jsonrpc_dispatcher", dispatcher, raising=False)

    # Isolate this test from transport post-import hooks and other instrumentors.
    monkeypatch.setattr(
        instrumentation,
        "register_post_import_hook",
        lambda callback, name: (
            callback(dispatcher)
            if native_client_context and name == dispatcher.__name__
            else None
        ),
    )
    original = BaseSession.send_request
    real_unwrap = instrumentation.unwrap
    monkeypatch.setattr(
        instrumentation,
        "unwrap",
        lambda module, attribute: (
            real_unwrap(module, attribute)
            if module in {"mcp.shared.session.BaseSession", dispatcher.__name__}
            else None
        ),
    )
    instrumentor = McpInstrumentor()
    try:
        instrumentor._instrument()
        assert BaseSession.send_request is not original
        if native_client_context:
            assert dispatcher.inject_trace_context is not original_inject
    finally:
        instrumentor._uninstrument()
    assert BaseSession.send_request is original
    assert dispatcher.inject_trace_context is original_inject
