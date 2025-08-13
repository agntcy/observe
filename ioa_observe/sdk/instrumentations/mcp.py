from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Collection, cast
import json
import logging
import re
from http import HTTPStatus

from opentelemetry import context, propagate
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.utils import unwrap
from opentelemetry.trace import get_tracer
from wrapt import ObjectProxy, register_post_import_hook, wrap_function_wrapper
from opentelemetry.trace.status import Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.semconv_ai import SpanAttributes
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE

from ioa_observe.sdk.utils.const import OBSERVE_ENTITY_OUTPUT, OBSERVE_ENTITY_INPUT

# Import MCP types at module level to avoid scoping issues
try:
    from mcp.types import JSONRPCMessage, JSONRPCRequest
except ImportError:
    JSONRPCMessage = None
    JSONRPCRequest = None

_instruments = ("mcp >= 1.6.0",)


class Config:
    exception_logger = None


def safe_execute(func):
    """Decorator that safely executes functions and logs exceptions."""
    logger = logging.getLogger(func.__module__)

    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.debug(f"Instrumentation failed in {func.__name__}: {e}")
            if Config.exception_logger:
                Config.exception_logger(e)

    return wrapper


class McpInstrumentor(BaseInstrumentor):
    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        tracer_provider = kwargs.get("tracer_provider")
        tracer = get_tracer(__name__, None, tracer_provider)

        # Register hooks for all MCP components
        self._register_hooks(tracer)

        # Wrap the main client method
        wrap_function_wrapper(
            "mcp.shared.session",
            "BaseSession.send_request",
            self._create_client_wrapper(tracer),
        )

    def _uninstrument(self, **kwargs):
        # Unwrap instrumented functions
        unwrap("mcp.client.stdio", "stdio_client")
        unwrap("mcp.server.stdio", "stdio_server")

    def _register_hooks(self, tracer):
        """Register all post-import hooks for MCP components."""
        components = [
            ("mcp.client.sse", "sse_client"),
            ("mcp.client.stdio", "stdio_client"),
            ("mcp.server.stdio", "stdio_server"),
            ("mcp.client.streamable_http", "streamablehttp_client"),
            ("mcp.server.sse", "SseServerTransport.connect_sse"),
            ("mcp.server.streamable_http", "StreamableHTTPServerTransport.connect"),
        ]

        for module, method in components:
            register_post_import_hook(
                lambda _, m=module, mt=method: wrap_function_wrapper(
                    m, mt, self._create_transport_wrapper(tracer)
                ),
                module,
            )

        # Special hooks
        register_post_import_hook(
            lambda _: wrap_function_wrapper(
                "mcp.server.session",
                "ServerSession.__init__",
                self._create_session_wrapper(tracer),
            ),
            "mcp.server.session",
        )

        register_post_import_hook(
            lambda _: wrap_function_wrapper(
                "mcp.server.rpc",
                "RpcServer.handle_request",
                self._create_server_wrapper(tracer),
            ),
            "mcp.server.rpc",
        )

    def _create_transport_wrapper(self, tracer):
        @asynccontextmanager
        async def wrapper(wrapped, instance, args, kwargs):
            async with wrapped(*args, **kwargs) as result:
                streams = self._extract_streams(result)
                if streams:
                    read_stream, write_stream = streams
                    yield (
                        InstrumentedStreamReader(read_stream, tracer),
                        InstrumentedStreamWriter(write_stream, tracer),
                    )
                else:
                    yield result

        return wrapper

    def _create_session_wrapper(self, tracer):
        def wrapper(wrapped, instance, args, kwargs):
            wrapped(*args, **kwargs)
            self._instrument_session_streams(instance, tracer)

        return wrapper

    def _create_client_wrapper(self, tracer):
        @safe_execute
        async def wrapper(wrapped, instance, args, kwargs):
            request_data = self._extract_request_data(args)
            span_name = f"{request_data.get('method', 'unknown')}.mcp"

            with tracer.start_as_current_span(span_name) as span:
                self._set_input_attributes(span, args)
                self._inject_trace_context(args, request_data)

                try:
                    result = await wrapped(*args, **kwargs)
                    self._handle_response(span, result)
                    return result
                except Exception as e:
                    self._handle_error(span, e)
                    raise

        return wrapper

    def _create_server_wrapper(self, tracer):
        @safe_execute
        async def wrapper(wrapped, instance, args, kwargs):
            context_info = self._extract_server_context(args)
            restore = self._attach_context(context_info)

            try:
                return await wrapped(*args, **kwargs)
            finally:
                if restore:
                    context.detach(restore)

        return wrapper

    def _extract_streams(self, result):
        """Extract read/write streams from transport result."""
        try:
            if isinstance(result, tuple) and len(result) >= 2:
                return result[:2]
            return None
        except (ValueError, TypeError):
            return None

    def _instrument_session_streams(self, instance, tracer):
        """Instrument session streams with context handling."""
        reader = getattr(instance, "_incoming_message_stream_reader", None)
        writer = getattr(instance, "_incoming_message_stream_writer", None)

        if reader and writer:
            setattr(
                instance,
                "_incoming_message_stream_reader",
                ContextStreamReader(reader, tracer),
            )
            setattr(
                instance,
                "_incoming_message_stream_writer",
                ContextStreamWriter(writer, tracer),
            )

    def _extract_request_data(self, args):
        """Extract method and params from request args."""
        if not args or not hasattr(args[0], "root"):
            return {}

        root = args[0].root
        return {
            "method": getattr(root, "method", None),
            "params": getattr(root, "params", None),
            "meta": getattr(getattr(root, "params", None), "meta", None),
        }

    def _set_input_attributes(self, span, args):
        """Set input attributes on span."""
        if args:
            span.set_attribute(OBSERVE_ENTITY_INPUT, serialize(args[0]))

    def _inject_trace_context(self, args, request_data):
        """Inject trace context into request metadata."""
        meta = request_data.get("meta")
        if meta and args:
            carrier = {}
            TraceContextTextMapPropagator().inject(carrier)
            meta.traceparent = carrier.get("traceparent")

            session_id = context.get_value("session_id")
            if session_id:
                meta.session_id = session_id

    def _handle_response(self, span, result):
        """Handle successful response."""
        span.set_attribute(OBSERVE_ENTITY_OUTPUT, serialize(result))

        if hasattr(result, "isError") and result.isError:
            self._set_error_status(span, result)
        else:
            span.set_status(Status(StatusCode.OK))

    def _handle_error(self, span, error):
        """Handle error response."""
        span.set_attribute(ERROR_TYPE, type(error).__name__)
        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, str(error)))

    def _set_error_status(self, span, result):
        """Set error status from result."""
        if hasattr(result, "content") and result.content:
            error_text = result.content[0].text
            span.set_status(Status(StatusCode.ERROR, error_text))

            error_type = self._extract_error_type(error_text)
            if error_type:
                span.set_attribute(ERROR_TYPE, error_type)

    def _extract_error_type(self, error_message):
        """Extract HTTP error type from error message."""
        if not isinstance(error_message, str):
            return None

        match = re.search(r"\b([45]\d{2})\b", error_message)
        if match:
            code = int(match.group(1))
            if 400 <= code <= 599:
                return HTTPStatus(code).name
        return None

    def _extract_server_context(self, args):
        """Extract context information from server request."""
        request = args[0] if args else None
        if not request or not hasattr(request, "params"):
            return None

        meta = getattr(request.params, "meta", None)
        if not meta:
            return None

        return {
            "session_id": getattr(meta, "session_id", None),
            "traceparent": getattr(meta, "traceparent", None),
        }

    def _attach_context(self, context_info):
        """Attach context from request metadata."""
        if not context_info:
            return None

        traceparent = context_info.get("traceparent")
        session_id = context_info.get("session_id")

        if not traceparent:
            return None

        carrier = {"traceparent": traceparent}
        ctx = TraceContextTextMapPropagator().extract(carrier=carrier)

        if session_id:
            ctx = context.set_value("session_id", session_id, ctx)

        return context.attach(ctx)


def serialize(obj, depth=0, max_depth=4):
    """Serialize object to JSON with depth limit."""
    if depth > max_depth:
        return "{}"

    try:
        # Try direct JSON serialization first
        json.dumps(obj)
        return json.dumps(obj)
    except (TypeError, ValueError):
        pass

    # Try pydantic serialization
    if hasattr(obj, "model_dump_json"):
        return obj.model_dump_json()

    # Manual serialization for complex objects
    if not hasattr(obj, "__dict__"):
        return "{}"

    result = {}
    for attr, value in obj.__dict__.items():
        if attr.startswith("_"):
            continue

        if isinstance(value, (bool, str, int, float, type(None))):
            result[attr] = value
        else:
            result[attr] = serialize(value, depth + 1, max_depth)

    return json.dumps(result)


class InstrumentedStreamReader(ObjectProxy):
    """Instrumented stream reader with tracing."""

    def __init__(self, wrapped, tracer):
        super().__init__(wrapped)
        self._tracer = tracer

    async def __aenter__(self):
        return await self.__wrapped__.__aenter__()

    async def __aexit__(self, exc_type, exc_value, tb):
        return await self.__wrapped__.__aexit__(exc_type, exc_value, tb)

    @safe_execute
    async def __aiter__(self):
        async for item in self.__wrapped__:
            request = self._extract_request(item)
            if not request or not isinstance(request, JSONRPCRequest):
                yield item
                continue

            # Handle trace context extraction
            if hasattr(request, "params") and request.params:
                meta = request.params.get("_meta")
                if meta:
                    ctx = propagate.extract(meta)
                    restore = context.attach(ctx)
                    try:
                        yield item
                        continue
                    finally:
                        context.detach(restore)

            yield item

    def _extract_request(self, item):
        """Extract request from stream item."""
        if JSONRPCMessage is None:
            return None

        if hasattr(item, "message"):
            message = cast(JSONRPCMessage, item.message)
            return message.root if message else None
        elif hasattr(item, "root"):
            message = cast(JSONRPCMessage, item)
            return message.root if message else None
        return None


class InstrumentedStreamWriter(ObjectProxy):
    """Instrumented stream writer with tracing."""

    def __init__(self, wrapped, tracer):
        super().__init__(wrapped)
        self._tracer = tracer

    async def __aenter__(self):
        return await self.__wrapped__.__aenter__()

    async def __aexit__(self, exc_type, exc_value, tb):
        return await self.__wrapped__.__aexit__(exc_type, exc_value, tb)

    @safe_execute
    async def send(self, item):
        request = self._extract_request(item)
        if not request:
            return await self.__wrapped__.send(item)

        with self._tracer.start_as_current_span("StreamWriter") as span:
            self._set_span_attributes(span, request)

            if isinstance(request, JSONRPCRequest):
                self._inject_context_metadata(request)

            return await self.__wrapped__.send(item)

    def _extract_request(self, item):
        """Extract request from stream item."""
        if JSONRPCMessage is None:
            return None

        if hasattr(item, "message"):
            message = cast(JSONRPCMessage, item.message)
            return message.root if message else None
        elif hasattr(item, "root"):
            message = cast(JSONRPCMessage, item)
            return message.root if message else None
        return None

    def _set_span_attributes(self, span, request):
        """Set span attributes from request."""
        if hasattr(request, "result"):
            span.set_attribute(
                SpanAttributes.MCP_RESPONSE_VALUE, serialize(request.result)
            )
            self._handle_error_result(span, request.result)

        if hasattr(request, "id"):
            span.set_attribute(SpanAttributes.MCP_REQUEST_ID, str(request.id))

    def _handle_error_result(self, span, result):
        """Handle error results in response."""
        if isinstance(result, dict) and result.get("isError"):
            content = result.get("content", [])
            if content:
                error_text = content[0].get("text", "")
                span.set_status(Status(StatusCode.ERROR, error_text))

                error_type = self._extract_error_type(error_text)
                if error_type:
                    span.set_attribute(ERROR_TYPE, error_type)

    def _extract_error_type(self, error_message):
        """Extract HTTP error type from message."""
        if not isinstance(error_message, str):
            return None

        match = re.search(r"\b([45]\d{2})\b", error_message)
        if match:
            code = int(match.group(1))
            if 400 <= code <= 599:
                return HTTPStatus(code).name
        return None

    def _inject_context_metadata(self, request):
        """Inject trace context into request metadata."""
        if not hasattr(request, "params") or not request.params:
            request.params = {}

        meta = request.params.setdefault("_meta", {})
        propagate.get_global_textmap().inject(meta)


@dataclass(frozen=True)
class ContextItem:
    """Container for items with context."""

    item: Any
    context: context.Context


class ContextStreamWriter(ObjectProxy):
    """Context-saving stream writer."""

    def __init__(self, wrapped, tracer):
        super().__init__(wrapped)
        self._tracer = tracer

    async def __aenter__(self):
        return await self.__wrapped__.__aenter__()

    async def __aexit__(self, exc_type, exc_value, tb):
        return await self.__wrapped__.__aexit__(exc_type, exc_value, tb)

    @safe_execute
    async def send(self, item):
        with self._tracer.start_as_current_span("ContextWriter") as span:
            self._set_request_attributes(span, item)
            ctx = context.get_current()
            return await self.__wrapped__.send(ContextItem(item, ctx))

    def _set_request_attributes(self, span, item):
        """Set request attributes on span."""
        if hasattr(item, "request_id"):
            span.set_attribute(SpanAttributes.MCP_REQUEST_ID, str(item.request_id))

        if hasattr(item, "request") and hasattr(item.request, "root"):
            root = item.request.root
            if hasattr(root, "method"):
                span.set_attribute(SpanAttributes.MCP_METHOD_NAME, root.method)
            if hasattr(root, "params"):
                span.set_attribute(
                    SpanAttributes.MCP_REQUEST_ARGUMENT, serialize(root.params)
                )


class ContextStreamReader(ObjectProxy):
    """Context-attaching stream reader."""

    def __init__(self, wrapped, tracer):
        super().__init__(wrapped)
        self._tracer = tracer

    async def __aenter__(self):
        return await self.__wrapped__.__aenter__()

    async def __aexit__(self, exc_type, exc_value, tb):
        return await self.__wrapped__.__aexit__(exc_type, exc_value, tb)

    async def __aiter__(self):
        async for item in self.__wrapped__:
            if isinstance(item, ContextItem):
                restore = context.attach(item.context)
                try:
                    yield item.item
                finally:
                    context.detach(restore)
            else:
                yield item
