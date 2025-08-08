# python
import json
import logging
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Collection, Any

from opentelemetry.instrumentation.utils import unwrap
from opentelemetry import context, propagate
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.trace import get_tracer, Tracer
from opentelemetry.trace.status import Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from wrapt import ObjectProxy, register_post_import_hook, wrap_function_wrapper
from opentelemetry.semconv_ai import SpanAttributes

from ioa_observe.sdk.utils.const import OBSERVE_ENTITY_OUTPUT, OBSERVE_ENTITY_INPUT

_instruments = ("mcp >= 1.6.0",)

@dataclass(slots=True, frozen=True)
class ItemWithContext:
    item: Any
    ctx: context.Context


def dont_throw(func):
    logger = logging.getLogger(func.__module__)

    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.debug("Error in %s: %s", func.__name__, traceback.format_exc())

    return wrapper


class McpInstrumentor(BaseInstrumentor):
    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        tracer = get_tracer(__name__, None, kwargs.get("tracer_provider"))

        def wrap_client_and_server(module, func, wrapper):
            register_post_import_hook(
                lambda _: wrap_function_wrapper(module, func, wrapper(tracer)), module
            )

        wrap_client_and_server("mcp.client.sse", "sse_client", self._transport_wrapper)
        wrap_client_and_server("mcp.server.sse", "SseServerTransport.connect_sse", self._transport_wrapper)
        wrap_client_and_server("mcp.client.stdio", "stdio_client", self._transport_wrapper)
        wrap_client_and_server("mcp.server.stdio", "stdio_server", self._transport_wrapper)
        wrap_client_and_server("mcp.server.rpc", "RpcServer.handle_request", self.patch_mcp_server)
        wrap_function_wrapper("mcp.shared.session", "BaseSession.send_request", self.patch_mcp_client(tracer))

    def _uninstrument(self, **kwargs):
        unwrap("mcp.client.stdio", "stdio_client")
        unwrap("mcp.server.stdio", "stdio_server")

    def _transport_wrapper(self, tracer):
        @asynccontextmanager
        async def traced_method(wrapped, instance, args, kwargs):
            async with wrapped(*args, **kwargs) as result:
                yield InstrumentedStreamReader(result[0], tracer), InstrumentedStreamWriter(result[1], tracer)

        return traced_method

    def patch_mcp_client(self, tracer: Tracer):
        @dont_throw
        async def traced_method(wrapped, instance, args, kwargs):
            meta = getattr(args[0].root.params, "meta", None) if args else None
            session_id = context.get_value("session_id")
            if meta and session_id:
                meta.session_id = session_id

            with tracer.start_as_current_span(f"{args[0].root.method}.mcp") as span:
                span.set_attribute(OBSERVE_ENTITY_INPUT, serialize(args[0]))
                if meta:
                    carrier = {}
                    TraceContextTextMapPropagator().inject(carrier)
                    meta.traceparent = carrier["traceparent"]
                try:
                    result = await wrapped(*args, **kwargs)
                    span.set_attribute(OBSERVE_ENTITY_OUTPUT, serialize(result))
                    if getattr(result, "isError", False):
                        span.set_status(Status(StatusCode.ERROR, str(result.content[0].text)))
                    return result
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    raise

        return traced_method

    def patch_mcp_server(self, tracer: Tracer):
        @dont_throw
        async def traced_method(wrapped, instance, args, kwargs):
            request = args[0] if args else None
            meta = getattr(request.params, "meta", None) if request else None
            carrier = {"traceparent": getattr(meta, "traceparent", None)} if meta else {}
            ctx = TraceContextTextMapPropagator().extract(carrier)
            if meta and (session_id := getattr(meta, "session_id", None)):
                ctx = context.set_value("session_id", session_id, ctx)
            restore = context.attach(ctx)

            try:
                return await wrapped(*args, **kwargs)
            finally:
                context.detach(restore)

        return traced_method

def serialize(request, depth=0, max_depth=4):
    """Serialize input args to MCP server into JSON.
    The function accepts input object and converts into JSON
    keeping depth in mind to prevent creating large nested JSON"""
    if depth > max_depth:
        return {}
    depth += 1

    def is_serializable(request):
        try:
            json.dumps(request)
            return True
        except Exception:
            return False

    if is_serializable(request):
        return json.dumps(request)
    else:
        result = {}
        try:
            if hasattr(request, "model_dump_json"):
                return request.model_dump_json()
            if hasattr(request, "__dict__"):
                for attrib in request.__dict__:
                    if not attrib.startswith("_"):
                        if type(request.__dict__[attrib]) in [
                            bool,
                            str,
                            int,
                            float,
                            type(None),
                        ]:
                            result[str(attrib)] = request.__dict__[attrib]
                        else:
                            result[str(attrib)] = serialize(
                                request.__dict__[attrib], depth
                            )
        except Exception:
            pass
        return json.dumps(result)


class InstrumentedStreamReader(ObjectProxy):
    def __init__(self, wrapped, tracer):
        super().__init__(wrapped)
        self._tracer = tracer

    async def __aiter__(self):
        async for item in self.__wrapped__:
            meta = getattr(item.params, "_meta", None) if hasattr(item, "params") else None
            ctx = propagate.extract(meta) if meta else context.get_current()
            restore = context.attach(ctx)
            try:
                yield item
            finally:
                context.detach(restore)


class InstrumentedStreamWriter(ObjectProxy):
    def __init__(self, wrapped, tracer):
        super().__init__(wrapped)
        self._tracer = tracer

    async def send(self, item):
        with self._tracer.start_as_current_span("RequestStreamWriter") as span:
            if hasattr(item, "request_id"):
                span.set_attribute(SpanAttributes.MCP_REQUEST_ID, f"{item.request_id}")
            if hasattr(item, "request"):
                if hasattr(item.request, "root"):
                    if hasattr(item.request.root, "method"):
                        span.set_attribute(
                            SpanAttributes.MCP_METHOD_NAME,
                            f"{item.request.root.method}",
                        )
                    if hasattr(item.request.root, "params"):
                        span.set_attribute(
                            SpanAttributes.MCP_REQUEST_ARGUMENT,
                            f"{serialize(item.request.root.params)}",
                        )
            ctx = context.get_current()
            return await self.__wrapped__.send(ItemWithContext(item, ctx))