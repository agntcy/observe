# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.util import find_spec
from typing import Any, AsyncGenerator, Callable, Collection, Tuple, cast, Union
import contextvars
import json
import logging
import re
import threading
import uuid
from http import HTTPStatus

from opentelemetry.context import get_value
from opentelemetry import context, propagate
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.utils import unwrap
from opentelemetry.trace import get_current_span, get_tracer, Tracer
from wrapt import ObjectProxy, register_post_import_hook, wrap_function_wrapper
from opentelemetry.trace.status import Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE

from ioa_observe.sdk import TracerWrapper
from ..config import is_content_tracing_enabled
from ..utils.const import (
    MCP_REQUEST_ID,
    MCP_METHOD_NAME,
    MCP_REQUEST_ARGUMENT,
    MCP_RESPONSE_VALUE,
    OBSERVE_ENTITY_OUTPUT,
    OBSERVE_ENTITY_INPUT,
)
from ..tracing.runtime_events import RuntimeEventName
from ..tracing.pre_call_events import (
    emit_tool_started_before_send,
    emit_tool_completed_after_send,
)
from ..tracing.message_payloads import serialize_message_payload
from ..tracing.context_utils import (
    _get_agent_linking_info,
    _get_outbound_session_id,
    _inject_outbound_context,
)
from ..tracing.topology import emit_topology_event, upsert_topology_edge
from ..tracing.capability_capture import record_capability_event
from ..version import __version__

_instruments = ("mcp >= 1.6.0",)
_kv_lock = threading.RLock()  # Thread-safety for kv_store operations


def _safe_get_current_actor_name() -> str:
    return str(get_value("workflow_name") or TracerWrapper.app_name or "unknown")


def _safe_int(value):
    return int(value) if str(value).isdigit() else None


def _request_field(request, name):
    return (
        request.get(name) if isinstance(request, dict) else getattr(request, name, None)
    )


def _request_meta(params):
    meta = (
        params.get("_meta")
        if isinstance(params, dict)
        else getattr(params, "meta", None)
    )
    if hasattr(meta, "model_dump"):
        return meta.model_dump(by_alias=True, exclude_none=True)
    return dict(meta) if isinstance(meta, dict) else {}


def _set_request_meta(request, params, meta):
    from mcp.types import JSONRPCRequest, RequestParams

    if params is None:
        if hasattr(type(request), "model_fields") and not isinstance(
            request, JSONRPCRequest
        ):
            data = request.model_dump(by_alias=True, exclude_none=True)
            data["params"] = {"_meta": meta}
            request.params = type(request).model_validate(data).params
            return
        params = {}
        if isinstance(request, dict):
            request["params"] = params
        else:
            request.params = params
    if isinstance(params, dict):
        params["_meta"] = meta
    elif isinstance(params, RequestParams):
        params.meta = RequestParams.model_validate({"_meta": meta}).meta
    else:
        params.meta = meta


def _restore_mcp_session_context(meta, ctx):
    from ioa_observe.sdk.client import kv_store

    session_id = meta.get("session.id")
    if not session_id or session_id == "None":
        return ctx
    ctx = context.set_value("session.id", session_id, context=ctx)
    traceparent = meta.get("traceparent")
    if traceparent:
        ctx = context.set_value("current_traceparent", traceparent, context=ctx)
    with _kv_lock:
        if traceparent:
            kv_store.set(f"execution.{traceparent}", session_id)
        for key in (
            "last_agent_span_id",
            "last_agent_trace_id",
            "last_agent_name",
            "agent_sequence",
        ):
            if meta.get(key):
                kv_store.set(f"session.{session_id}.{key}", meta[key])
        if meta.get("fork_id") and meta.get("agent_sequence"):
            sequence = int(meta["agent_sequence"])
            prefix = f"session.{session_id}.agents.{sequence}"
            kv_store.set(f"{prefix}.fork_id", meta["fork_id"])
            for key, field in (
                ("fork_parent_seq", "parent_seq"),
                ("fork_branch_index", "branch_index"),
            ):
                if meta.get(key):
                    kv_store.set(f"{prefix}.{field}", meta[key])
    return ctx


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
    fallback = None
    for attr in ("_write_stream", "write_stream", "_read_stream", "read_stream"):
        peer_name = _safe_stringify_mcp_peer(getattr(instance, attr, None))
        if peer_name in {"mcp", "unknown"}:
            fallback = peer_name
        elif peer_name:
            return peer_name

    peer_name = _safe_stringify_mcp_peer(instance)
    if peer_name in {"mcp", "unknown"}:
        fallback = peer_name
    elif peer_name:
        return peer_name
    server_info = getattr(instance, "_observe_server_info", None)
    if server_info:
        return server_info["name"]
    for value in getattr(instance, "__dict__", {}).values():
        peer_name = _safe_stringify_mcp_peer(value)
        if peer_name in {"mcp", "unknown"}:
            fallback = peer_name
        elif peer_name:
            return peer_name
    return fallback


def _capture_mcp_server_info(instance, result):
    info = _request_field(result, "serverInfo") or _request_field(result, "server_info")
    if info is None:
        metadata = _request_field(result, "meta") or _request_field(result, "_meta")
        if hasattr(metadata, "model_dump"):
            metadata = metadata.model_dump(by_alias=True, exclude_none=True)
        if isinstance(metadata, dict):
            info = metadata.get("io.modelcontextprotocol/serverInfo")
    name = _request_field(info, "name") if info is not None else None
    if isinstance(name, str) and name:
        try:
            instance._observe_server_info = {"name": name}
        except (AttributeError, TypeError):
            logging.warning("Unable to cache MCP server identity", exc_info=True)


def _mcp_message_input(params) -> str | None:
    payload = serialize_message_payload(params, protocol="MCP")
    if payload is None:
        return None
    data = json.loads(payload)
    if isinstance(data, dict):
        # Request metadata contains propagation state rather than call arguments.
        data.pop("_meta", None)
        data.pop("meta", None)
    return json.dumps(data, sort_keys=True)


def _emit_mcp_send_topology_event(
    observe_meta, operation: str, message_id=None, *, message_input=None
) -> dict[str, str | bool | int | float] | None:
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
        evidence="transport:mcp",
        confidence=1.0,
        source_agent_span_id=observe_meta.get("last_agent_span_id"),
        source_agent_trace_id=observe_meta.get("last_agent_trace_id"),
    )
    event = emit_topology_event(
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
        mcp_server_name=observe_meta.get("mcp.server.name"),
        message_input=message_input,
        source_agent_span_id=observe_meta.get("last_agent_span_id"),
        source_agent_trace_id=observe_meta.get("last_agent_trace_id"),
    )
    if event is not None:
        observe_meta["event.id"] = event["event.id"]
    return event


_PENDING_TOOL_CALL: contextvars.ContextVar = contextvars.ContextVar(
    "observe_pending_mcp_tool_call", default=None
)


def _stage_mcp_tool_call_started(
    params, session_id: str, request_id, parent_event_id: str | None = None
) -> dict[str, Any] | None:
    """Remember the tool and its arguments until the MCP library injects its trace context.

    The library opens its own ``MCP send`` span and injects the traceparent that the request carries
    inside it, so the event has to be emitted there for its span to be the one on the wire.
    """
    data = params
    if not is_content_tracing_enabled():
        _PENDING_TOOL_CALL.set(None)
        return
    if not isinstance(data, dict):
        dump = getattr(params, "model_dump", None)
        data = dump(mode="json", by_alias=True) if callable(dump) else {}
    name = data.get("name")
    if isinstance(name, str) and name:
        pending = {
            "session_id": session_id,
            "name": name,
            "arguments": data.get("arguments") or {},
            "call_id": str(request_id) if request_id is not None else None,
            "parent_event_id": parent_event_id,
            "started": None,
            "span": None,
        }
        _PENDING_TOOL_CALL.set(pending)
        return pending


def _emit_staged_mcp_tool_call_started() -> None:
    pending = _PENDING_TOOL_CALL.get(None)
    if not pending:
        return
    _PENDING_TOOL_CALL.set(None)
    if not is_content_tracing_enabled():
        return
    pending["span"] = get_current_span()
    pending["started"] = emit_tool_started_before_send(
        pending["session_id"],
        pending["name"],
        pending["arguments"],
        call_id=pending["call_id"],
        parent_event_id=pending["parent_event_id"],
    )


def _emit_mcp_receive_topology_event(
    observe_meta, operation: str, message_id=None, *, message_input=None
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
    message_id = observe_meta.get("observe.message.id") or message_id

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
        evidence="transport:mcp",
        confidence=1.0,
        source_agent_span_id=observe_meta.get("last_agent_span_id"),
        source_agent_trace_id=observe_meta.get("last_agent_trace_id"),
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
        mcp_server_name=observe_meta.get("mcp.server.name"),
        message_input=message_input,
        source_agent_span_id=observe_meta.get("last_agent_span_id"),
        source_agent_trace_id=observe_meta.get("last_agent_trace_id"),
        parent_event_id=observe_meta.get("event.id"),
    )


def _emit_mcp_completed_event(sent, message_input, result, *, server_name=None) -> None:
    if sent is None:
        return
    emit_topology_event(
        RuntimeEventName.MCP_MESSAGE_COMPLETED.value,
        session_id=sent["session.id"],
        source=sent["source.agent"],
        target=sent["target.agent"],
        operation=sent["operation.name"],
        message_id=sent.get("message.id"),
        sequence=sent.get("sequence"),
        fork_id=sent.get("fork.id"),
        protocol="mcp",
        mcp_server_name=server_name or sent.get("mcp.server.name"),
        source_agent_span_id=sent.get("source.agent.span.id"),
        source_agent_trace_id=sent.get("source.agent.trace.id"),
        parent_event_id=sent["event.id"],
        message_input=message_input if is_content_tracing_enabled() else None,
        message_output=serialize_message_payload(result, protocol="MCP"),
    )


class McpInstrumentor(BaseInstrumentor):
    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        tracer_provider = kwargs.get("tracer_provider")
        tracer = get_tracer(__name__, __version__, tracer_provider)
        self._instrumentation_active = True
        self._server_context_wrapped = False
        self._client_context_wrapped = False
        native_server_context = find_spec("mcp.server._otel") is not None

        def instrument_server_context(module):
            if self._instrumentation_active and not self._server_context_wrapped:
                wrap_function_wrapper(
                    module.__name__,
                    "OpenTelemetryMiddleware.__call__",
                    self._server_context_wrapper,
                )
                self._server_context_wrapped = True

        if native_server_context:
            register_post_import_hook(instrument_server_context, "mcp.server._otel")

        def instrument_dispatcher(module):
            # The library owns the span of an outgoing request and injects its trace context inside it.
            if (
                self._instrumentation_active
                and not self._client_context_wrapped
                and hasattr(module, "inject_trace_context")
            ):
                wrap_function_wrapper(
                    module.__name__,
                    "inject_trace_context",
                    self._inject_with_staged_tool_call,
                )
                self._client_context_wrapped = True

        register_post_import_hook(
            instrument_dispatcher, "mcp.shared.jsonrpc_dispatcher"
        )

        def wrap_if_present(module_name, attribute, wrapper):
            module = __import__(module_name, fromlist=[attribute.split(".", 1)[0]])
            target = module
            for part in attribute.split("."):
                if not hasattr(target, part):
                    return
                target = getattr(target, part)
            wrap_function_wrapper(module_name, attribute, wrapper)

        register_post_import_hook(
            lambda _: wrap_if_present(
                "mcp.client.sse", "sse_client", self._transport_wrapper(tracer)
            ),
            "mcp.client.sse",
        )
        register_post_import_hook(
            lambda _: wrap_if_present(
                "mcp.server.sse",
                "SseServerTransport.connect_sse",
                self._transport_wrapper(
                    tracer, capture_inbound_context=not native_server_context
                ),
            ),
            "mcp.server.sse",
        )
        register_post_import_hook(
            lambda _: wrap_if_present(
                "mcp.client.stdio", "stdio_client", self._transport_wrapper(tracer)
            ),
            "mcp.client.stdio",
        )
        register_post_import_hook(
            lambda _: wrap_if_present(
                "mcp.server.stdio",
                "stdio_server",
                self._transport_wrapper(
                    tracer, capture_inbound_context=not native_server_context
                ),
            ),
            "mcp.server.stdio",
        )
        register_post_import_hook(
            lambda _: wrap_if_present(
                "mcp.server.session",
                "ServerSession.__init__",
                self._base_session_init_wrapper(tracer),
            ),
            "mcp.server.session",
        )
        register_post_import_hook(
            lambda _: wrap_if_present(
                "mcp.client.streamable_http",
                "streamablehttp_client",
                self._transport_wrapper(tracer),
            ),
            "mcp.client.streamable_http",
        )
        register_post_import_hook(
            lambda _: wrap_if_present(
                "mcp.client.streamable_http",
                "streamable_http_client",
                self._transport_wrapper(tracer),
            ),
            "mcp.client.streamable_http",
        )
        register_post_import_hook(
            lambda _: wrap_if_present(
                "mcp.server.streamable_http",
                "StreamableHTTPServerTransport.connect",
                self._transport_wrapper(
                    tracer, capture_inbound_context=not native_server_context
                ),
            ),
            "mcp.server.streamable_http",
        )
        if find_spec("mcp.shared.session"):
            wrap_function_wrapper(
                "mcp.shared.session",
                "BaseSession.send_request",
                self.patch_mcp_client(tracer),
            )
        else:
            wrap_function_wrapper(
                "mcp.client.session",
                "ClientSession.send_request",
                self.patch_mcp_client(tracer),
            )

    def _uninstrument(self, **kwargs):
        self._instrumentation_active = False
        from sys import modules

        if self._client_context_wrapped and "mcp.shared.jsonrpc_dispatcher" in modules:
            unwrap("mcp.shared.jsonrpc_dispatcher", "inject_trace_context")
        self._client_context_wrapped = False
        if self._server_context_wrapped and "mcp.server._otel" in modules:
            unwrap("mcp.server._otel.OpenTelemetryMiddleware", "__call__")
        self._server_context_wrapped = False
        if find_spec("mcp.shared.session"):
            unwrap("mcp.shared.session.BaseSession", "send_request")
        elif find_spec("mcp.client.session"):
            unwrap("mcp.client.session.ClientSession", "send_request")

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

    def _transport_wrapper(self, tracer, *, capture_inbound_context=True):
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
                        (
                            InstrumentedStreamReader(
                                read_stream, tracer, peer_name=peer_name
                            )
                            if capture_inbound_context
                            else read_stream
                        ),
                        InstrumentedStreamWriter(
                            write_stream, tracer, peer_name=peer_name
                        ),
                    )
                except ValueError:
                    try:
                        read_stream, write_stream, get_session_id_callback = result
                        yield (
                            (
                                InstrumentedStreamReader(
                                    read_stream, tracer, peer_name=peer_name
                                )
                                if capture_inbound_context
                                else read_stream
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

    @staticmethod
    async def _server_context_wrapper(wrapped, instance, args, kwargs):
        request_context = args[0] if args else kwargs["ctx"]
        call_next = args[1] if len(args) > 1 else kwargs["call_next"]

        # Restore Observe context inside MCP's native request span.
        async def call_with_context(request_context):
            meta = _request_meta(request_context)
            token = context.attach(
                _restore_mcp_session_context(meta, context.get_current())
            )
            try:
                _emit_mcp_receive_topology_event(
                    meta,
                    request_context.method,
                    message_id=(
                        str(request_context.request_id)
                        if request_context.request_id is not None
                        else None
                    ),
                    message_input=_mcp_message_input(request_context.params),
                )
                return await call_next(request_context)
            finally:
                context.detach(token)

        return await wrapped(request_context, call_with_context)

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
        native_client_context = find_spec("mcp.shared.jsonrpc_dispatcher") is not None

        async def traced_method(wrapped, instance, args, kwargs):
            from ioa_observe.sdk.client import kv_store

            method = None
            params = None
            request_id = None
            request = args[0] if args else kwargs.get("request")
            request_root = getattr(request, "root", request)
            if request_root is not None:
                method = _request_field(request_root, "method")
                params = _request_field(request_root, "params")
                request_id = _request_field(request_root, "id")

            with tracer.start_as_current_span(f"{method}.mcp") as span:
                try:
                    span.set_attribute(OBSERVE_ENTITY_INPUT, f"{serialize(args[0])}")
                except Exception:
                    pass

                observe_meta = _request_meta(params)
                _inject_outbound_context(observe_meta)
                traceparent = observe_meta.get("traceparent")
                session_id = _get_outbound_session_id()
                if traceparent and session_id:
                    with _kv_lock:
                        kv_store.set(f"execution.{traceparent}", session_id)
                if session_id:
                    observe_meta["session.id"] = session_id

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

                server_name = _request_field(
                    getattr(instance, "_observe_server_info", None), "name"
                )
                if isinstance(server_name, str) and server_name:
                    observe_meta["mcp.server.name"] = server_name
                else:
                    observe_meta.pop("mcp.server.name", None)

                message_input = _mcp_message_input(params if params is not None else {})
                message_id = (
                    str(request_id) if request_id is not None else uuid.uuid4().hex
                )
                observe_meta["observe.message.id"] = message_id
                sent = _emit_mcp_send_topology_event(
                    observe_meta,
                    str(method or "send_request"),
                    message_id=message_id,
                    message_input=message_input,
                )

                pending_token = _PENDING_TOOL_CALL.set(None)
                pending_tool = None
                try:
                    if method == "tools/call" and session_id:
                        pending_tool = _stage_mcp_tool_call_started(
                            params, session_id, message_id, observe_meta.get("event.id")
                        )
                    if observe_meta and request_root is not None:
                        _set_request_meta(request_root, params, observe_meta)
                    if not native_client_context:
                        _emit_staged_mcp_tool_call_started()
                    result = await wrapped(*args, **kwargs)
                    if method in {"initialize", "tools/list", "tools/call"}:
                        _capture_mcp_server_info(instance, result)
                    if method == "tools/list":
                        server_info = getattr(instance, "_observe_server_info", None)
                        record_capability_event(
                            "mcp.tools.discovered",
                            result,
                            provider_id=_extract_mcp_peer_name(instance),
                            source="client-discovered",
                            attributes={"mcp.server.name": server_info["name"]}
                            if server_info
                            else None,
                        )
                    try:
                        span.set_attribute(
                            OBSERVE_ENTITY_OUTPUT,
                            serialize(result),
                        )
                    except Exception:
                        pass
                    is_error = getattr(
                        result, "is_error", getattr(result, "isError", False)
                    )
                    if is_error:
                        if len(result.content) > 0:
                            span.set_status(
                                Status(StatusCode.ERROR, f"{result.content[0].text}")
                            )
                            error_type = get_error_type(result.content[0].text)
                            if error_type is not None:
                                span.set_attribute(ERROR_TYPE, error_type)
                    else:
                        span.set_status(Status(StatusCode.OK))
                    if pending_tool:
                        emit_tool_completed_after_send(
                            pending_tool["started"],
                            result=result,
                            span=pending_tool["span"],
                        )
                    _emit_mcp_completed_event(
                        sent,
                        message_input,
                        result,
                        server_name=_request_field(
                            getattr(instance, "_observe_server_info", None), "name"
                        ),
                    )
                    return result
                except BaseException as e:
                    if pending_tool:
                        emit_tool_completed_after_send(
                            pending_tool["started"], error=e, span=pending_tool["span"]
                        )
                    span.set_attribute(ERROR_TYPE, type(e).__name__)
                    span.record_exception(e)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    raise
                finally:
                    _PENDING_TOOL_CALL.reset(pending_token)

        return traced_method

    @staticmethod
    def _inject_with_staged_tool_call(wrapped, instance, args, kwargs):
        result = wrapped(*args, **kwargs)
        _emit_staged_mcp_tool_call_started()
        return result


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
        self._self_tracer = tracer
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
                message = cast(McpJSONRPCMessage, item.message)
                request = getattr(message, "root", message)
            else:
                request = getattr(item, "root", item)

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
                        message_input=_mcp_message_input(request.params),
                    )

                    if carrier and traceparent:
                        # Extract W3C trace context + baggage
                        ctx = TraceContextTextMapPropagator().extract(carrier=carrier)
                        ctx = W3CBaggagePropagator().extract(
                            carrier=carrier, context=ctx
                        )

                        ctx = _restore_mcp_session_context(observe_meta, ctx)
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
        self._self_tracer = tracer
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
            message = cast(McpJSONRPCMessage, item.message)
            request = getattr(message, "root", message)
        else:
            request = getattr(item, "root", item)

        # The request span is active here. The writer's diagnostic child span
        # must not replace the context used by the send event and wire request.
        outbound_context = context.get_current()
        try:
            with self._self_tracer.start_as_current_span(
                "ResponseStreamWriter"
            ) as span:
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
                meta = _request_meta(request.params)
                injection_context = outbound_context
                if not get_current_span(
                    outbound_context
                ).get_span_context().is_valid and not meta.get("traceparent"):
                    # Standalone stream writes still get a trace context when
                    # there is no request span or already-propagated context.
                    injection_context = context.get_current()
                extra = {}
                propagate.get_global_textmap().inject(extra, context=injection_context)
                for key, value in extra.items():
                    if key not in ("traceparent", "tracestate", "baggage"):
                        meta[key] = value
                _inject_outbound_context(meta, current_context=injection_context)
                request.params["_meta"] = meta
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
        self._self_tracer = tracer
        self._self_observe_peer_name = peer_name

    async def __aenter__(self) -> Any:
        return await self.__wrapped__.__aenter__()

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        return await self.__wrapped__.__aexit__(exc_type, exc_value, traceback)

    async def send(self, item: Any) -> Any:
        try:
            with self._self_tracer.start_as_current_span("RequestStreamWriter") as span:
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
        self._self_tracer = tracer
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
