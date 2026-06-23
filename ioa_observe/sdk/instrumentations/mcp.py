# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.util import find_spec
from typing import Any, AsyncGenerator, Callable, Collection, Tuple, cast, Union
import json
import logging
import re
import threading
from http import HTTPStatus

from opentelemetry.context import get_value
from opentelemetry import baggage, context, propagate
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.utils import unwrap
from opentelemetry.trace import get_tracer, Tracer
from wrapt import ObjectProxy, register_post_import_hook, wrap_function_wrapper
from opentelemetry.trace.status import Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE

from ioa_observe.sdk import TracerWrapper
from ..utils.const import (
    MCP_REQUEST_ID,
    MCP_METHOD_NAME,
    MCP_REQUEST_ARGUMENT,
    MCP_RESPONSE_VALUE,
    OBSERVE_ENTITY_OUTPUT,
    OBSERVE_ENTITY_INPUT,
)
from ..tracing.runtime_events import RuntimeEventName
from ..tracing.topology import emit_topology_event, upsert_topology_edge
from ..version import __version__

_instruments = ("mcp >= 1.6.0",)
_kv_lock = threading.RLock()  # Thread-safety for kv_store operations


def _safe_get_current_actor_name() -> str:
    return str(get_value("workflow_name") or TracerWrapper.app_name or "unknown")


def _safe_int(value):
    return int(value) if str(value).isdigit() else None


def _safe_stringify_mcp_peer(candidate) -> str | None:
    if candidate is None:
        return None
    if isinstance(candidate, str):
        return candidate
    if isinstance(candidate, dict):
        for key in ("observe_peer_name", "url", "endpoint", "command", "name", "id"):
            value = candidate.get(key)
            if value:
                return str(value)
        return None

    for attr in (
        "observe_peer_name",
        "_self_observe_peer_name",
        "url",
        "endpoint",
        "server_url",
        "name",
        "id",
    ):
        value = getattr(candidate, attr, None)
        if value:
            return str(value)

    command = getattr(candidate, "command", None)
    if command:
        return f"stdio:{command}"
    return None


def _infer_mcp_peer_name(args, kwargs) -> str | None:
    for candidate in list(args) + list(kwargs.values()):
        peer_name = _safe_stringify_mcp_peer(candidate)
        if peer_name:
            return peer_name
    return None


def _extract_mcp_peer_name(instance) -> str | None:
    for attr in ("_write_stream", "write_stream", "_read_stream", "read_stream"):
        peer_name = _safe_stringify_mcp_peer(getattr(instance, attr, None))
        if peer_name:
            return peer_name

    for value in getattr(instance, "__dict__", {}).values():
        peer_name = _safe_stringify_mcp_peer(value)
        if peer_name:
            return peer_name
    return None


def _emit_mcp_send_topology_event(
    observe_meta, operation: str, message_id=None
) -> None:
    session_id = observe_meta.get("session.id")
    target = observe_meta.get("target_agent")
    if not session_id or not target:
        return

    source = (
        observe_meta.get("source_agent")
        or observe_meta.get("last_agent_name")
        or _safe_get_current_actor_name()
    )
    sequence = _safe_int(observe_meta.get("agent_sequence"))
    fork_id = observe_meta.get("fork_id")

    upsert_topology_edge(
        session_id,
        source,
        target,
        transport="mcp",
        status="sent",
        operation=operation,
        message_id=message_id,
        sequence=sequence,
        fork_id=fork_id,
        kind="mcp_message",
    )
    emit_topology_event(
        RuntimeEventName.MCP_MESSAGE_SENT.value,
        session_id=session_id,
        include_snapshot=True,
        source=source,
        target=target,
        operation=operation,
        message_id=message_id,
        sequence=sequence,
        fork_id=fork_id,
        protocol="mcp",
    )


def _emit_mcp_receive_topology_event(
    observe_meta, operation: str, message_id=None
) -> None:
    session_id = observe_meta.get("session.id")
    if not session_id:
        return

    source = (
        observe_meta.get("source_agent")
        or observe_meta.get("last_agent_name")
        or "unknown"
    )
    target = observe_meta.get("target_agent") or _safe_get_current_actor_name()
    sequence = _safe_int(observe_meta.get("agent_sequence"))
    fork_id = observe_meta.get("fork_id")

    upsert_topology_edge(
        session_id,
        source,
        target,
        transport="mcp",
        status="received",
        operation=operation,
        message_id=message_id,
        sequence=sequence,
        fork_id=fork_id,
        kind="mcp_message",
    )
    emit_topology_event(
        RuntimeEventName.MCP_MESSAGE_RECEIVED.value,
        session_id=session_id,
        include_snapshot=True,
        source=source,
        target=target,
        operation=operation,
        message_id=message_id,
        sequence=sequence,
        fork_id=fork_id,
        protocol="mcp",
    )


class McpInstrumentor(BaseInstrumentor):
    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        tracer_provider = kwargs.get("tracer_provider")
        tracer = get_tracer(__name__, __version__, tracer_provider)

        register_post_import_hook(
            lambda _: wrap_function_wrapper(
                "mcp.client.sse", "sse_client", self._transport_wrapper(tracer)
            ),
            "mcp.client.sse",
        )
        register_post_import_hook(
            lambda _: wrap_function_wrapper(
                "mcp.server.sse",
                "SseServerTransport.connect_sse",
                self._transport_wrapper(tracer),
            ),
            "mcp.server.sse",
        )
        register_post_import_hook(
            lambda _: wrap_function_wrapper(
                "mcp.client.stdio", "stdio_client", self._transport_wrapper(tracer)
            ),
            "mcp.client.stdio",
        )
        register_post_import_hook(
            lambda _: wrap_function_wrapper(
                "mcp.server.stdio", "stdio_server", self._transport_wrapper(tracer)
            ),
            "mcp.server.stdio",
        )
        register_post_import_hook(
            lambda _: wrap_function_wrapper(
                "mcp.server.session",
                "ServerSession.__init__",
                self._base_session_init_wrapper(tracer),
            ),
            "mcp.server.session",
        )
        register_post_import_hook(
            lambda _: wrap_function_wrapper(
                "mcp.client.streamable_http",
                "streamablehttp_client",
                self._transport_wrapper(tracer),
            ),
            "mcp.client.streamable_http",
        )
        register_post_import_hook(
            lambda _: wrap_function_wrapper(
                "mcp.client.streamable_http",
                "streamable_http_client",
                self._transport_wrapper(tracer),
            ),
            "mcp.client.streamable_http",
        )
        register_post_import_hook(
            lambda _: wrap_function_wrapper(
                "mcp.server.streamable_http",
                "StreamableHTTPServerTransport.connect",
                self._transport_wrapper(tracer),
            ),
            "mcp.server.streamable_http",
        )
        wrap_function_wrapper(
            "mcp.shared.session",
            "BaseSession.send_request",
            self.patch_mcp_client(tracer),
        )

    def _uninstrument(self, **kwargs):
        unwrap("mcp.shared.session", "BaseSession.send_request")

        unwrap("mcp.client.stdio", "stdio_client")
        unwrap("mcp.server.stdio", "stdio_server")

        if find_spec("mcp.client.sse"):
            try:
                unwrap("mcp.client.sse", "sse_client")
            except Exception:
                pass
        if find_spec("mcp.server.sse"):
            try:
                unwrap("mcp.server.sse", "SseServerTransport.connect_sse")
            except Exception:
                pass
        if find_spec("mcp.client.streamable_http"):
            try:
                unwrap("mcp.client.streamable_http", "streamablehttp_client")
            except Exception:
                pass
            try:
                unwrap("mcp.client.streamable_http", "streamable_http_client")
            except Exception:
                pass
        if find_spec("mcp.server.streamable_http"):
            try:
                unwrap(
                    "mcp.server.streamable_http",
                    "StreamableHTTPServerTransport.connect",
                )
            except Exception:
                pass

    def _transport_wrapper(self, tracer):
        @asynccontextmanager
        async def traced_method(
            wrapped: Callable[..., Any], instance: Any, args: Any, kwargs: Any
        ) -> AsyncGenerator[
            Union[
                Tuple[InstrumentedStreamReader, InstrumentedStreamWriter],
                Tuple[InstrumentedStreamReader, InstrumentedStreamWriter, Any],
            ],
            None,
        ]:
            async with wrapped(*args, **kwargs) as result:
                peer_name = _infer_mcp_peer_name(args, kwargs)
                try:
                    read_stream, write_stream = result
                    yield (
                        InstrumentedStreamReader(
                            read_stream, tracer, peer_name=peer_name
                        ),
                        InstrumentedStreamWriter(
                            write_stream, tracer, peer_name=peer_name
                        ),
                    )
                except ValueError:
                    try:
                        read_stream, write_stream, get_session_id_callback = result
                        yield (
                            InstrumentedStreamReader(
                                read_stream, tracer, peer_name=peer_name
                            ),
                            InstrumentedStreamWriter(
                                write_stream, tracer, peer_name=peer_name
                            ),
                            get_session_id_callback,
                        )
                    except Exception as e:
                        logging.warning(
                            f"mcp instrumentation _transport_wrapper exception: {e}"
                        )
                        yield result
                except Exception as e:
                    logging.warning(
                        f"mcp instrumentation transport_wrapper exception: {e}"
                    )
                    yield result

        return traced_method

    def _base_session_init_wrapper(self, tracer):
        def traced_method(
            wrapped: Callable[..., None], instance: Any, args: Any, kwargs: Any
        ) -> None:
            wrapped(*args, **kwargs)
            reader = getattr(instance, "_incoming_message_stream_reader", None)
            writer = getattr(instance, "_incoming_message_stream_writer", None)
            peer_name = _safe_stringify_mcp_peer(reader) or _safe_stringify_mcp_peer(
                writer
            )
            if reader and writer:
                setattr(
                    instance,
                    "_incoming_message_stream_reader",
                    ContextAttachingStreamReader(reader, tracer, peer_name=peer_name),
                )
                setattr(
                    instance,
                    "_incoming_message_stream_writer",
                    ContextSavingStreamWriter(writer, tracer, peer_name=peer_name),
                )

        return traced_method

    def patch_mcp_client(self, tracer: Tracer):
        async def traced_method(wrapped, instance, args, kwargs):
            from ioa_observe.sdk.client import kv_store
            from ioa_observe.sdk.tracing import get_current_traceparent
            from ioa_observe.sdk.tracing.context_utils import _get_agent_linking_info

            method = None
            params = None
            request_id = None
            if len(args) > 0 and hasattr(args[0].root, "method"):
                method = args[0].root.method
            if len(args) > 0 and hasattr(args[0].root, "params"):
                params = args[0].root.params
            if len(args) > 0 and hasattr(args[0].root, "id"):
                request_id = args[0].root.id

            with tracer.start_as_current_span(f"{method}.mcp") as span:
                try:
                    span.set_attribute(OBSERVE_ENTITY_INPUT, f"{serialize(args[0])}")
                except Exception:
                    pass

                traceparent = get_current_traceparent()
                session_id = None
                if traceparent:
                    with _kv_lock:
                        session_id = kv_store.get(f"execution.{traceparent}")
                    if not session_id:
                        session_id = get_value("session.id")
                        if session_id:
                            with _kv_lock:
                                kv_store.set(f"execution.{traceparent}", session_id)

                # Build observe metadata dict (matching A2A pattern)
                observe_meta = {}

                # Inject W3C trace context + baggage
                TraceContextTextMapPropagator().inject(carrier=observe_meta)
                W3CBaggagePropagator().inject(carrier=observe_meta)

                if traceparent:
                    observe_meta["traceparent"] = traceparent
                if session_id:
                    observe_meta["session.id"] = session_id
                    baggage.set_baggage(f"execution.{traceparent}", session_id)

                # Add agent linking info for cross-process propagation
                if session_id:
                    agent_linking_info = _get_agent_linking_info(session_id)
                    if agent_linking_info.get("last_agent_span_id"):
                        observe_meta["last_agent_span_id"] = agent_linking_info[
                            "last_agent_span_id"
                        ]
                    if agent_linking_info.get("last_agent_trace_id"):
                        observe_meta["last_agent_trace_id"] = agent_linking_info[
                            "last_agent_trace_id"
                        ]
                    if agent_linking_info.get("last_agent_name"):
                        observe_meta["last_agent_name"] = agent_linking_info[
                            "last_agent_name"
                        ]
                    if agent_linking_info.get("agent_sequence"):
                        observe_meta["agent_sequence"] = agent_linking_info[
                            "agent_sequence"
                        ]
                    # Add fork context for cross-process fork detection
                    if agent_linking_info.get("fork_id"):
                        observe_meta["fork_id"] = agent_linking_info["fork_id"]
                    if agent_linking_info.get("fork_parent_seq"):
                        observe_meta["fork_parent_seq"] = agent_linking_info[
                            "fork_parent_seq"
                        ]
                    if agent_linking_info.get("fork_branch_index"):
                        observe_meta["fork_branch_index"] = agent_linking_info[
                            "fork_branch_index"
                        ]

                source_agent = (
                    observe_meta.get("last_agent_name")
                    or _safe_get_current_actor_name()
                )
                if source_agent:
                    observe_meta["source_agent"] = source_agent
                target_agent = _extract_mcp_peer_name(instance)
                if target_agent:
                    observe_meta["target_agent"] = target_agent

                # Inject observe metadata into request params._meta
                if observe_meta and params and len(args) > 0:
                    try:
                        # Get existing meta or create new one
                        existing_meta = getattr(params, "meta", None)
                        meta_kwargs = {}
                        if existing_meta and hasattr(existing_meta, "model_dump"):
                            meta_kwargs = existing_meta.model_dump(exclude_none=True)
                        elif isinstance(existing_meta, dict):
                            meta_kwargs = dict(existing_meta)
                        meta_kwargs.update(observe_meta)
                        from mcp.types import RequestParams as McpRequestParamsType

                        args[0].root.params.meta = McpRequestParamsType.Meta(
                            **meta_kwargs
                        )
                    except Exception:
                        # Fallback: set as dict (may trigger Pydantic warning)
                        try:
                            args[0].root.params.meta = observe_meta
                        except Exception:
                            pass

                _emit_mcp_send_topology_event(
                    observe_meta,
                    str(method or "send_request"),
                    message_id=str(request_id) if request_id is not None else None,
                )

                try:
                    result = await wrapped(*args, **kwargs)
                    try:
                        span.set_attribute(
                            OBSERVE_ENTITY_OUTPUT,
                            serialize(result),
                        )
                    except Exception:
                        pass
                    if hasattr(result, "isError") and result.isError:
                        if len(result.content) > 0:
                            span.set_status(
                                Status(StatusCode.ERROR, f"{result.content[0].text}")
                            )
                            error_type = get_error_type(result.content[0].text)
                            if error_type is not None:
                                span.set_attribute(ERROR_TYPE, error_type)
                    else:
                        span.set_status(Status(StatusCode.OK))
                    return result
                except Exception as e:
                    span.set_attribute(ERROR_TYPE, type(e).__name__)
                    span.record_exception(e)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    raise

        return traced_method


def get_error_type(error_message):
    if not isinstance(error_message, str):
        return None
    match = re.search(r"\b(4\d{2}|5\d{2})\b", error_message)
    if match:
        num = int(match.group())
        if 400 <= num <= 599:
            return HTTPStatus(num).name
        else:
            return None
    else:
        return None


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


class InstrumentedStreamReader(ObjectProxy):  # type: ignore
    def __init__(self, wrapped, tracer, peer_name=None):
        super().__init__(wrapped)
        self._tracer = tracer
        self._self_observe_peer_name = peer_name

    async def __aenter__(self) -> Any:
        return await self.__wrapped__.__aenter__()

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        return await self.__wrapped__.__aexit__(exc_type, exc_value, traceback)

    async def __aiter__(self) -> AsyncGenerator[Any, None]:
        from mcp.shared.message import SessionMessage as McpSessionMessage
        from mcp.types import JSONRPCMessage as McpJSONRPCMessage
        from mcp.types import JSONRPCRequest as McpJSONRPCRequest

        async for item in self.__wrapped__:
            if isinstance(item, McpSessionMessage):
                request = cast(McpJSONRPCMessage, item.message).root
            elif type(item) is McpJSONRPCMessage:
                request = cast(McpJSONRPCMessage, item).root
            else:
                yield item
                continue

            if not isinstance(request, McpJSONRPCRequest):
                yield item
                continue

            if request.params:
                # Check both _meta and meta fields
                meta = request.params.get("_meta") or getattr(
                    request.params, "meta", None
                )

                if meta:
                    if isinstance(meta, dict):
                        session_id = meta.get("session.id")
                        traceparent = meta.get("traceparent")
                        carrier = meta
                    else:
                        session_id = getattr(meta, "session.id", None)
                        traceparent = getattr(meta, "traceparent", None)
                        # Convert object to dict for propagate.extract
                        carrier = {}
                        if hasattr(meta, "model_dump"):
                            carrier = meta.model_dump(exclude_none=True)
                        elif session_id:
                            carrier["session.id"] = session_id

                    observe_meta = carrier if isinstance(carrier, dict) else {}
                    _emit_mcp_receive_topology_event(
                        observe_meta,
                        str(getattr(request, "method", None) or "mcp.request"),
                        message_id=str(getattr(request, "id", None))
                        if getattr(request, "id", None) is not None
                        else None,
                    )

                    if carrier and traceparent:
                        # Extract W3C trace context + baggage
                        ctx = TraceContextTextMapPropagator().extract(carrier=carrier)
                        ctx = W3CBaggagePropagator().extract(
                            carrier=carrier, context=ctx
                        )

                        # Restore session_id and agent linking info (matching A2A server handler)
                        if session_id and session_id != "None":
                            from ioa_observe.sdk.client import kv_store
                            from ioa_observe.sdk.tracing import set_session_id

                            set_session_id(session_id, traceparent=traceparent)
                            with _kv_lock:
                                kv_store.set(f"execution.{traceparent}", session_id)

                                # Restore agent linking info for cross-process span linking
                                observe_meta = (
                                    carrier if isinstance(carrier, dict) else {}
                                )
                                last_agent_span_id = observe_meta.get(
                                    "last_agent_span_id"
                                )
                                last_agent_trace_id = observe_meta.get(
                                    "last_agent_trace_id"
                                )
                                last_agent_name = observe_meta.get("last_agent_name")
                                agent_sequence = observe_meta.get("agent_sequence")

                                if last_agent_span_id:
                                    kv_store.set(
                                        f"session.{session_id}.last_agent_span_id",
                                        last_agent_span_id,
                                    )
                                if last_agent_trace_id:
                                    kv_store.set(
                                        f"session.{session_id}.last_agent_trace_id",
                                        last_agent_trace_id,
                                    )
                                if last_agent_name:
                                    kv_store.set(
                                        f"session.{session_id}.last_agent_name",
                                        last_agent_name,
                                    )
                                if agent_sequence:
                                    kv_store.set(
                                        f"session.{session_id}.agent_sequence",
                                        agent_sequence,
                                    )

                                # Restore fork context for cross-process fork detection
                                fork_id = observe_meta.get("fork_id")
                                fork_parent_seq = observe_meta.get("fork_parent_seq")
                                fork_branch_index = observe_meta.get(
                                    "fork_branch_index"
                                )
                                if fork_id and agent_sequence:
                                    seq = int(agent_sequence)
                                    kv_store.set(
                                        f"session.{session_id}.agents.{seq}.fork_id",
                                        fork_id,
                                    )
                                    if fork_parent_seq:
                                        kv_store.set(
                                            f"session.{session_id}.agents.{seq}.parent_seq",
                                            fork_parent_seq,
                                        )
                                    if fork_branch_index:
                                        kv_store.set(
                                            f"session.{session_id}.agents.{seq}.branch_index",
                                            fork_branch_index,
                                        )

                        restore = context.attach(ctx)
                        try:
                            yield item
                            continue
                        finally:
                            context.detach(restore)
            yield item


class InstrumentedStreamWriter(ObjectProxy):  # type: ignore
    def __init__(self, wrapped, tracer, peer_name=None):
        super().__init__(wrapped)
        self._tracer = tracer
        self._self_observe_peer_name = peer_name

    async def __aenter__(self) -> Any:
        return await self.__wrapped__.__aenter__()

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        return await self.__wrapped__.__aexit__(exc_type, exc_value, traceback)

    async def send(self, item: Any) -> Any:
        from mcp.shared.message import SessionMessage as McpSessionMessage
        from mcp.types import JSONRPCMessage as McpJSONRPCMessage
        from mcp.types import JSONRPCRequest as McpJSONRPCRequest

        if isinstance(item, McpSessionMessage):
            request = cast(McpJSONRPCMessage, item.message).root
        elif type(item) is McpJSONRPCMessage:
            request = cast(McpJSONRPCMessage, item).root
        else:
            # Always forward unrecognized message types
            return await self.__wrapped__.send(item)

        try:
            with self._tracer.start_as_current_span("ResponseStreamWriter") as span:
                if hasattr(request, "result"):
                    span.set_attribute(
                        MCP_RESPONSE_VALUE, f"{serialize(request.result)}"
                    )
                    if isinstance(request.result, dict) and "isError" in request.result:
                        if request.result["isError"] is True:
                            span.set_status(
                                Status(
                                    StatusCode.ERROR,
                                    f"{request.result['content'][0]['text']}",
                                )
                            )
                            error_type = get_error_type(
                                request.result["content"][0]["text"]
                            )
                            if error_type is not None:
                                span.set_attribute(ERROR_TYPE, error_type)
                if hasattr(request, "id"):
                    span.set_attribute(MCP_REQUEST_ID, f"{request.id}")

                if not isinstance(request, McpJSONRPCRequest):
                    return await self.__wrapped__.send(item)
                if not request.params:
                    request.params = {}
                meta = request.params.setdefault("_meta", {})

                propagate.get_global_textmap().inject(meta)
                return await self.__wrapped__.send(item)
        except Exception as e:
            logging.warning(f"mcp instrumentation ResponseStreamWriter exception: {e}")
            # Ensure message is always delivered even if tracing fails
            return await self.__wrapped__.send(item)


@dataclass(slots=True, frozen=True)
class ItemWithContext:
    item: Any
    ctx: context.Context


class ContextSavingStreamWriter(ObjectProxy):  # type: ignore
    def __init__(self, wrapped, tracer, peer_name=None):
        super().__init__(wrapped)
        self._tracer = tracer
        self._self_observe_peer_name = peer_name

    async def __aenter__(self) -> Any:
        return await self.__wrapped__.__aenter__()

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        return await self.__wrapped__.__aexit__(exc_type, exc_value, traceback)

    async def send(self, item: Any) -> Any:
        try:
            with self._tracer.start_as_current_span("RequestStreamWriter") as span:
                if hasattr(item, "request_id"):
                    span.set_attribute(MCP_REQUEST_ID, f"{item.request_id}")
                if hasattr(item, "request"):
                    if hasattr(item.request, "root"):
                        if hasattr(item.request.root, "method"):
                            span.set_attribute(
                                MCP_METHOD_NAME,
                                f"{item.request.root.method}",
                            )
                        if hasattr(item.request.root, "params"):
                            span.set_attribute(
                                MCP_REQUEST_ARGUMENT,
                                f"{serialize(item.request.root.params)}",
                            )
                ctx = context.get_current()
                return await self.__wrapped__.send(ItemWithContext(item, ctx))
        except Exception as e:
            logging.warning(f"mcp instrumentation RequestStreamWriter exception: {e}")
            ctx = context.get_current()
            return await self.__wrapped__.send(ItemWithContext(item, ctx))


class ContextAttachingStreamReader(ObjectProxy):  # type: ignore
    def __init__(self, wrapped, tracer, peer_name=None):
        super().__init__(wrapped)
        self._tracer = tracer
        self._self_observe_peer_name = peer_name

    async def __aenter__(self) -> Any:
        return await self.__wrapped__.__aenter__()

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        return await self.__wrapped__.__aexit__(exc_type, exc_value, traceback)

    async def __aiter__(self) -> AsyncGenerator[Any, None]:
        async for item in self.__wrapped__:
            item_with_context = cast(ItemWithContext, item)
            restore = context.attach(item_with_context.ctx)
            try:
                yield item_with_context.item
            finally:
                context.detach(restore)
