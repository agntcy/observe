# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import asyncio
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


def test_patch_mcp_client_injects_realtime_protocol_metadata_and_events():
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

    snapshot = get_live_topology_snapshot(session_id)
    assert snapshot["edges"][0]["id"] == "mcp:planner->mcp://math-server"
    assert snapshot["edges"][0]["status"] == "sent"


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

    tool_events = [event for event in events if event["event.name"].startswith("tool.")]
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
        event["event.name"] for event in tool_events
    ]
    for record, event in zip(tool_logs, tool_events):
        assert record.attributes["session.id"] == event["session.id"]
        assert record.attributes["tool.name"] == event["tool.name"]
        assert format(record.trace_id, "032x") == event["trace.id"]
        assert format(record.span_id, "016x") == event["span.id"]


def test_mcp_stream_reader_restores_tool_session_without_leaking_context():
    types = pytest.importorskip("mcp.types")
    from ioa_observe.sdk.decorators import tool

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
        method="tools/call", jsonrpc="2.0", id=1, params={"_meta": meta}
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
        assert "message.id" not in sent  # Native SDK IDs are optional at send_request.
    else:
        assert sent["message.id"] == "req-1"
    assert not any(key.startswith("source.") for key in meta)


@pytest.mark.parametrize("with_params", [True, False])
def test_mcp_actual_base_session_serializes_meta_and_progress_tokens(with_params):
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
                await session._response_streams[request.id].send(
                    types.JSONRPCResponse(jsonrpc="2.0", id=request.id, result={})
                )

        read_writer, read_stream = anyio.create_memory_object_stream(1)
        session = ClientSession(
            read_stream,
            InstrumentedStreamWriter(Sink(), tracer, peer_name="mcp://receiver"),
        )
        request = types.ClientRequest(
            types.PingRequest(
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
        result = await patched(
            session.send_request,
            session,
            (request, types.EmptyResult),
            {"progress_callback": progress},
        )
        assert isinstance(result, types.EmptyResult)
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
    if with_params:
        assert meta["custom"] == "kept"
    assert meta["traceparent"].split("-")[1:3] == [sent["trace.id"], sent["span.id"]]


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


def test_mcp_uninstrument_restores_actual_base_session_send_request(monkeypatch):
    pytest.importorskip("mcp.shared.session")
    from mcp.shared.session import BaseSession
    from ioa_observe.sdk.instrumentations import mcp as instrumentation

    # Isolate this test from transport post-import hooks and other instrumentors.
    monkeypatch.setattr(
        instrumentation, "register_post_import_hook", lambda *args: None
    )
    original = BaseSession.send_request
    real_unwrap = instrumentation.unwrap
    monkeypatch.setattr(
        instrumentation,
        "unwrap",
        lambda module, attribute: (
            real_unwrap(module, attribute)
            if module == "mcp.shared.session.BaseSession"
            else None
        ),
    )
    instrumentor = McpInstrumentor()
    try:
        instrumentor._instrument()
        assert BaseSession.send_request is not original
    finally:
        instrumentor._uninstrument()
    assert BaseSession.send_request is original
