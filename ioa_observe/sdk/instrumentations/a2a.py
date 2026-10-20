# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from typing import Collection
import functools
import importlib
import inspect
import json
import logging
import threading

from opentelemetry.context import get_value
from opentelemetry import baggage, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from ioa_observe.sdk import TracerWrapper
from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.config import (
    is_content_tracing_enabled,
    is_realtime_observability_enabled,
)
from ioa_observe.sdk.tracing import set_session_id
from ioa_observe.sdk.tracing.context_utils import (
    _get_agent_linking_info,
    _get_outbound_session_id,
    _inject_outbound_context,
)
from ioa_observe.sdk.tracing.pre_call_events import (
    PRECALL_EVENT_ID_KEY,
    emit_tool_started_before_send,
)
from ioa_observe.sdk.tracing.message_payloads import serialize_message_payload
from ioa_observe.sdk.tracing.topology import emit_topology_event, upsert_topology_edge
from ioa_observe.sdk.tracing.capability_capture import record_capability_event

_instruments = ("a2a-sdk >= 0.3.0",)
_global_tracer = None
_kv_lock = threading.RLock()  # Add thread-safety for kv_store operations

# Track original methods for uninstrumentation
_original_methods = {}


def _record_card(card, source, *, session_id=None, attributes=None):
    if card is not None:
        if hasattr(card, "DESCRIPTOR"):
            from google.protobuf.json_format import MessageToDict

            card = MessageToDict(card)
        url = card.get("url") if isinstance(card, dict) else getattr(card, "url", None)
        if not url and isinstance(card, dict):
            interfaces = card.get("supportedInterfaces", [])
            if interfaces:
                url = interfaces[0].get("url")
        record_capability_event(
            "a2a.agent_card",
            card,
            provider_id=url,
            source=source,
            session_id=session_id,
            attributes=attributes,
        )


def _record_message_card(instance, observe_meta, source, message_id, operation):
    try:
        for field in ("_card", "_agent_card", "agent_card"):
            card = getattr(instance, field, None)
            if card is not None:
                _record_card(
                    card,
                    source,
                    session_id=observe_meta.get("session_id"),
                    attributes={
                        "message.id": message_id,
                        "operation.name": operation,
                        "network.protocol.name": "a2a",
                    },
                )
                return
    except Exception:
        logging.warning("Unable to capture A2A message card")


def _safe_get_metadata(container):
    candidates = [
        getattr(container, "params", None),
        getattr(container, "message", None),
        container,
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        metadata = getattr(candidate, "metadata", None)
        if isinstance(metadata, dict):
            return metadata
        if metadata is not None and hasattr(metadata, "DESCRIPTOR"):
            from google.protobuf.json_format import MessageToDict

            return MessageToDict(metadata)
    return {}


def _set_metadata(container, metadata):
    metadata = dict(metadata)
    candidates = [
        getattr(container, "params", None),
        getattr(container, "message", None),
        container,
    ]
    for candidate in candidates:
        if candidate is None or not hasattr(candidate, "metadata"):
            continue
        current = getattr(candidate, "metadata", None)
        if isinstance(current, dict):
            current.clear()
            current.update(metadata)
        elif current is not None and hasattr(current, "DESCRIPTOR"):
            current.Clear()
            current.update(metadata)
        else:
            setattr(candidate, "metadata", metadata)
        return container
    return container


def _safe_get_observe_meta(container) -> dict:
    return dict(_safe_get_metadata(container).get("observe", {}))


def _safe_get_message_id(container) -> str | None:
    try:
        params = getattr(container, "params", container)
        message = getattr(params, "message", None) or getattr(
            container, "message", None
        )
        if message is not None:
            return (
                getattr(message, "messageId", None)
                or getattr(message, "message_id", None)
                or getattr(message, "id", None)
            )
    except Exception:
        pass
    return None


def _safe_get_target_name(container=None, fallback=None) -> str | None:
    candidates = [container, fallback]
    for candidate in candidates:
        if candidate is None:
            continue
        for attr in ("agent_card", "_card", "agent", "target_agent", "target"):
            nested = getattr(candidate, attr, None)
            if nested is not None:
                candidates.append(nested)
        for attr in ("name", "id", "agent_id", "agent_name"):
            value = getattr(candidate, attr, None)
            if value:
                return str(value)
    return None


def _safe_get_message_text(request) -> str:
    params = getattr(request, "params", None)
    message = (
        getattr(request, "message", None) or getattr(params, "message", None) or request
    )
    parts = getattr(message, "parts", None) or []
    texts = []
    for part in parts:
        text = (
            part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
        )
        if isinstance(text, str) and text:
            texts.append(text)
    return "\n".join(texts)


def _safe_get_current_actor_name() -> str:
    return str(get_value("workflow_name") or TracerWrapper.app_name or "unknown")


def _serialize_message_payload(value) -> str | None:
    return serialize_message_payload(value, protocol="A2A")


def _message_input(request) -> str | None:
    params = getattr(request, "params", request)
    message = getattr(params, "message", None)
    payload = _serialize_message_payload(message)
    if payload is None:
        return None
    decoded = json.loads(payload)
    if isinstance(decoded, dict) and isinstance(decoded.get("metadata"), dict):
        # Propagation metadata changes after emission and is not message content.
        decoded["metadata"].pop("observe", None)
    return json.dumps(decoded, sort_keys=True)


def _capture_a2a_response(original, operation: str, *, streaming: bool = False):
    def capture_request(request, instance):
        metadata = _safe_get_observe_meta(request)
        if not is_realtime_observability_enabled() or not metadata.get("session_id"):
            return None
        return {
            "session_id": metadata["session_id"],
            "source": metadata.get("last_agent_name") or _safe_get_current_actor_name(),
            "target": _safe_get_target_name(request, instance) or "unknown",
            "message_id": _safe_get_message_id(request),
            "message_input": _message_input(request),
            "parent_event_id": metadata.get("event.id"),
            "source_agent_span_id": metadata.get("last_agent_span_id"),
            "source_agent_trace_id": metadata.get("last_agent_trace_id"),
            "fork_id": metadata.get("fork_id"),
            "sequence": (
                int(metadata["agent_sequence"])
                if str(metadata.get("agent_sequence")).isdigit()
                else None
            ),
            "context": TraceContextTextMapPropagator().extract(metadata),
        }

    def complete(captured, response):
        if captured is None:
            return
        context = captured.pop("context")
        # Reuse the outgoing span identity without extending its network lifetime.
        with trace.use_span(trace.get_current_span(context), end_on_exit=False):
            emit_topology_event(
                "a2a.message.completed",
                **captured,
                protocol="a2a",
                operation=operation,
                message_output=_serialize_message_payload(response),
            )

    if streaming:

        @functools.wraps(original)
        async def stream(instance, request, *args, **kwargs):
            captured = capture_request(request, instance)
            last_response = None
            async for response in original(instance, request, *args, **kwargs):
                last_response = response
                yield response
            complete(captured, last_response)

        return stream

    @functools.wraps(original)
    async def send(instance, request, *args, **kwargs):
        captured = capture_request(request, instance)
        response = await original(instance, request, *args, **kwargs)
        complete(captured, response)
        return response

    return send


def _emit_a2a_send_topology_event(request, client_instance, operation: str) -> None:
    observe_meta = _safe_get_observe_meta(request)
    session_id = observe_meta.get("session_id")
    if not session_id:
        return

    sender = observe_meta.get("last_agent_name") or _safe_get_current_actor_name()
    target = _safe_get_target_name(request, client_instance) or "unknown"
    message_id = _safe_get_message_id(request)
    _record_message_card(
        client_instance,
        observe_meta,
        "client-discovered",
        message_id,
        operation,
    )
    agent_sequence = observe_meta.get("agent_sequence")
    fork_id = observe_meta.get("fork_id")
    sequence = int(agent_sequence) if str(agent_sequence).isdigit() else None

    upsert_topology_edge(
        session_id,
        sender,
        target,
        transport="a2a",
        status="sent",
        operation=operation,
        message_id=message_id,
        sequence=sequence,
        fork_id=fork_id,
        kind="a2a_message",
        evidence="transport:a2a",
        confidence=1.0,
        source_agent_span_id=observe_meta.get("last_agent_span_id"),
        source_agent_trace_id=observe_meta.get("last_agent_trace_id"),
    )
    event = emit_topology_event(
        "a2a.message.sent",
        session_id=session_id,
        include_snapshot=True,
        source=sender,
        target=target,
        operation=operation,
        message_id=message_id,
        sequence=sequence,
        fork_id=fork_id,
        protocol="a2a",
        message_input=_message_input(request),
        source_agent_span_id=observe_meta.get("last_agent_span_id"),
        source_agent_trace_id=observe_meta.get("last_agent_trace_id"),
    )
    if event is not None:
        observe_meta["event.id"] = event["event.id"]
        _set_observe_metadata(request, observe_meta)
    # The text of the message, on the span that the outgoing trace context carries, before it is sent.
    if is_content_tracing_enabled():
        precall_event_id = emit_tool_started_before_send(
            session_id,
            f"a2a_message:{target}",
            {"text": _safe_get_message_text(request)},
            call_id=message_id,
            agent_name=sender,
            parent_event_id=event["event.id"] if event is not None else None,
        )
        if precall_event_id:
            # Next to ``event.id`` (the ``message.sent``): the id of the event that holds the content.
            observe_meta[PRECALL_EVENT_ID_KEY] = precall_event_id
            _set_observe_metadata(request, observe_meta)


def _set_observe_metadata(request, observe_meta) -> None:
    metadata = dict(_safe_get_metadata(request))
    metadata["observe"] = observe_meta
    _set_metadata(request, metadata)


def _emit_a2a_receive_topology_event(params, handler_instance, operation: str) -> None:
    observe_meta = _safe_get_observe_meta(params)
    session_id = observe_meta.get("session_id")
    if not session_id:
        return

    source = observe_meta.get("last_agent_name") or "unknown"
    target = _safe_get_target_name(handler_instance) or _safe_get_current_actor_name()
    message_id = _safe_get_message_id(params)
    _record_message_card(
        handler_instance,
        observe_meta,
        "server-published",
        message_id,
        operation,
    )
    agent_sequence = observe_meta.get("agent_sequence")
    fork_id = observe_meta.get("fork_id")
    sequence = int(agent_sequence) if str(agent_sequence).isdigit() else None

    upsert_topology_edge(
        session_id,
        source,
        target,
        transport="a2a",
        status="received",
        operation=operation,
        message_id=message_id,
        sequence=sequence,
        fork_id=fork_id,
        kind="a2a_message",
        evidence="transport:a2a",
        confidence=1.0,
        source_agent_span_id=observe_meta.get("last_agent_span_id"),
        source_agent_trace_id=observe_meta.get("last_agent_trace_id"),
    )
    emit_topology_event(
        "a2a.message.received",
        session_id=session_id,
        include_snapshot=True,
        source=source,
        target=target,
        operation=operation,
        message_id=message_id,
        sequence=sequence,
        fork_id=fork_id,
        protocol="a2a",
        message_input=_message_input(params),
        source_agent_span_id=observe_meta.get("last_agent_span_id"),
        source_agent_trace_id=observe_meta.get("last_agent_trace_id"),
        parent_event_id=observe_meta.get("event.id"),
    )


def _inject_observe_metadata(request, span_name: str):
    """
    Helper function to inject observability metadata into request.
    This is shared between legacy and new client instrumentation.
    """
    traceparent = _inject_outbound_context({}).get("traceparent")
    session_id = _get_outbound_session_id()
    if traceparent:
        if session_id:
            kv_store.set(f"execution.{traceparent}", session_id)

    # Get agent linking info for cross-process propagation (agent handoff event)
    agent_linking_info = _get_agent_linking_info(session_id) if session_id else {}

    metadata = _safe_get_metadata(request)

    observe_meta = dict(metadata.get("observe", {}))

    # Inject W3C trace context + baggage into observe_meta
    _inject_outbound_context(observe_meta)

    if traceparent:
        observe_meta["traceparent"] = traceparent
    if session_id:
        observe_meta["session_id"] = session_id
        baggage.set_baggage(f"execution.{traceparent}", session_id)

    # Add agent linking info for cross-process span linking
    if agent_linking_info.get("last_agent_span_id"):
        observe_meta["last_agent_span_id"] = agent_linking_info["last_agent_span_id"]
    if agent_linking_info.get("last_agent_trace_id"):
        observe_meta["last_agent_trace_id"] = agent_linking_info["last_agent_trace_id"]
    if agent_linking_info.get("last_agent_name"):
        observe_meta["last_agent_name"] = agent_linking_info["last_agent_name"]
    if agent_linking_info.get("agent_sequence"):
        observe_meta["agent_sequence"] = agent_linking_info["agent_sequence"]

    # Add fork context for cross-process fork detection
    if agent_linking_info.get("fork_id"):
        observe_meta["fork_id"] = agent_linking_info["fork_id"]
    if agent_linking_info.get("fork_parent_seq"):
        observe_meta["fork_parent_seq"] = agent_linking_info["fork_parent_seq"]
    if agent_linking_info.get("fork_branch_index"):
        observe_meta["fork_branch_index"] = agent_linking_info["fork_branch_index"]

    metadata["observe"] = observe_meta

    try:
        request = _set_metadata(request, metadata)
    except Exception:
        try:
            if hasattr(request, "params"):
                request = request.model_copy(
                    update={
                        "params": request.params.model_copy(
                            update={"metadata": metadata}
                        )
                    }
                )
            else:
                request = request.model_copy(update={"metadata": metadata})
        except Exception:
            pass

    return request


class A2AInstrumentor(BaseInstrumentor):
    def __init__(self):
        super().__init__()
        global _global_tracer
        _global_tracer = TracerWrapper().get_tracer()

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        import importlib

        if importlib.util.find_spec("a2a") is None:
            raise ImportError("No module named 'a2a-sdk'. Please install it first.")

        # Instrument new A2AClient (BaseClient) from a2a.client.base_client (v0.3.0+)
        self._instrument_base_client()

        # Instrument legacy A2AClient for backward compatibility
        self._instrument_legacy_client()

        # Instrument server handler
        self._instrument_server_handler()
        self._instrument_cards()

        # Instrument slima2a if available
        self._instrument_slima2a()

    def _instrument_cards(self):
        self._card_methods = []
        targets = (
            ("a2a.client.card_resolver", "A2ACardResolver", "get_agent_card"),
            ("a2a.client.base_client", "BaseClient", "get_card"),
            ("a2a.client.base_client", "BaseClient", "get_extended_agent_card"),
            ("a2a.client.legacy", "A2AClient", "get_card"),
            ("a2a.server.request_handlers", "DefaultRequestHandler", "__init__"),
            (
                "a2a.server.request_handlers",
                "DefaultRequestHandler",
                "on_get_extended_agent_card",
            ),
            ("a2a.server.apps", "A2AStarletteApplication", "__init__"),
            ("a2a.server.apps", "A2AFastAPIApplication", "__init__"),
            (
                "a2a.server.request_handlers.jsonrpc_handler",
                "JSONRPCHandler",
                "get_authenticated_extended_card",
            ),
            ("a2a.server.apps", "A2AStarletteApplication", "_handle_get_agent_card"),
            (
                "a2a.server.apps",
                "A2AStarletteApplication",
                "_handle_get_authenticated_extended_agent_card",
            ),
        )
        for module_name, class_name, method in targets:
            try:
                cls = getattr(importlib.import_module(module_name), class_name)
            except (ImportError, AttributeError):
                continue
            original = getattr(cls, method, None)
            if original is None:
                continue
            if method == "__init__":
                signature = inspect.signature(original)

                def constructor(original=original, signature=signature):
                    @functools.wraps(original)
                    def wrapped(instance, *args, **kwargs):
                        original(instance, *args, **kwargs)
                        try:
                            bound = signature.bind(instance, *args, **kwargs).arguments
                            for field in ("agent_card", "extended_agent_card"):
                                _record_card(bound.get(field), "server-published")
                        except Exception:
                            logging.warning("Unable to capture registered A2A card")

                    return wrapped

                wrapper = constructor()
            elif inspect.iscoroutinefunction(original):
                source = (
                    "client-discovered"
                    if module_name.startswith("a2a.client")
                    else "server-published"
                )

                def result_wrapper(original=original, source=source):
                    @functools.wraps(original)
                    async def wrapped(instance, *args, **kwargs):
                        result = await original(instance, *args, **kwargs)
                        try:
                            card = (
                                json.loads(result.body)
                                if hasattr(result, "body") and result.status_code == 200
                                else result
                                if not hasattr(result, "body")
                                else None
                            )
                            if hasattr(card, "root"):
                                card = getattr(card.root, "result", None)
                            _record_card(card, source)
                        except Exception:
                            logging.warning("Unable to capture returned A2A card")
                        return result

                    return wrapped

                wrapper = result_wrapper()
            else:
                continue
            self._card_methods.append((cls, method, original, method in cls.__dict__))
            setattr(cls, method, wrapper)

    def _instrument_base_client(self):
        """Instrument the new A2AClient (BaseClient) pattern introduced in v0.3.0."""
        import importlib

        # Check if the new base_client module exists
        if importlib.util.find_spec("a2a.client.base_client") is None:
            return

        try:
            from a2a.client.base_client import BaseClient as BaseA2AClient
        except ImportError:
            try:
                from a2a.client.base_client import A2AClient as BaseA2AClient
            except ImportError:
                return

        # Instrument send_message
        if hasattr(BaseA2AClient, "send_message"):
            original_send_message = BaseA2AClient.send_message
            _original_methods["BaseA2AClient.send_message"] = original_send_message
            original_send_message = _capture_a2a_response(
                original_send_message,
                "send_message",
                streaming=inspect.isasyncgenfunction(original_send_message),
            )

            if inspect.isasyncgenfunction(original_send_message):

                @functools.wraps(original_send_message)
                async def instrumented_send_message(self, request, *args, **kwargs):
                    with _global_tracer.start_as_current_span(
                        "a2a.client.send_message"
                    ):
                        request = _inject_observe_metadata(
                            request, "a2a.client.send_message"
                        )
                        _emit_a2a_send_topology_event(request, self, "send_message")
                    async for response in original_send_message(
                        self, request, *args, **kwargs
                    ):
                        yield response

            else:

                @functools.wraps(original_send_message)
                async def instrumented_send_message(self, request, *args, **kwargs):
                    with _global_tracer.start_as_current_span(
                        "a2a.client.send_message"
                    ):
                        request = _inject_observe_metadata(
                            request, "a2a.client.send_message"
                        )
                        _emit_a2a_send_topology_event(request, self, "send_message")
                    return await original_send_message(self, request, *args, **kwargs)

            BaseA2AClient.send_message = instrumented_send_message

        # Instrument send_message_streaming
        if hasattr(BaseA2AClient, "send_message_streaming"):
            original_send_message_streaming = BaseA2AClient.send_message_streaming
            _original_methods["BaseA2AClient.send_message_streaming"] = (
                original_send_message_streaming
            )
            original_send_message_streaming = _capture_a2a_response(
                original_send_message_streaming,
                "send_message_streaming",
                streaming=True,
            )

            @functools.wraps(original_send_message_streaming)
            async def instrumented_send_message_streaming(
                self, request, *args, **kwargs
            ):
                nonlocal original_send_message_streaming
                with _global_tracer.start_as_current_span(
                    "a2a.client.send_message_streaming"
                ):
                    request = _inject_observe_metadata(
                        request, "a2a.client.send_message_streaming"
                    )
                    _emit_a2a_send_topology_event(
                        request, self, "send_message_streaming"
                    )
                # This is an async generator, so we need to yield from it
                async for response in original_send_message_streaming(
                    self, request, *args, **kwargs
                ):
                    yield response

            BaseA2AClient.send_message_streaming = instrumented_send_message_streaming

    def _instrument_legacy_client(self):
        """Instrument the legacy A2AClient for backward compatibility."""

        # Try to import from legacy location first (v0.3.0+)
        legacy_client = None
        legacy_module_path = None

        try:
            from a2a.client.legacy import A2AClient as LegacyA2AClient

            legacy_client = LegacyA2AClient
            legacy_module_path = "a2a.client.legacy"
        except ImportError:
            # Fall back to old import path (pre-v0.3.0)
            try:
                from a2a.client import A2AClient as LegacyA2AClient

                # Check if this is the legacy client (has send_message method directly)
                if hasattr(LegacyA2AClient, "send_message"):
                    legacy_client = LegacyA2AClient
                    legacy_module_path = "a2a.client"
            except ImportError:
                pass

        if legacy_client is None:
            return

        # Store original methods for uninstrumentation
        original_send_message = legacy_client.send_message
        _original_methods[f"{legacy_module_path}.A2AClient.send_message"] = (
            original_send_message
        )
        original_send_message = _capture_a2a_response(
            original_send_message, "send_message"
        )

        @functools.wraps(original_send_message)
        async def instrumented_send_message(self, request, *args, **kwargs):
            # Put context into A2A message metadata instead of HTTP headers
            with _global_tracer.start_as_current_span("a2a.send_message"):
                traceparent = _inject_outbound_context({}).get("traceparent")
                session_id = _get_outbound_session_id()
                last_agent_span_id = None
                last_agent_trace_id = None
                last_agent_name = None
                agent_sequence = None
                if traceparent:
                    if session_id:
                        kv_store.set(f"execution.{traceparent}", session_id)
                    # Get agent linking info for cross-process propagation
                    if session_id:
                        with _kv_lock:
                            last_agent_span_id = (
                                kv_store.get(f"session.{session_id}.last_agent_span_id")
                                or None
                            )
                            last_agent_trace_id = (
                                kv_store.get(
                                    f"session.{session_id}.last_agent_trace_id"
                                )
                                or None
                            )
                            last_agent_name = (
                                kv_store.get(f"session.{session_id}.last_agent_name")
                                or None
                            )
                            agent_sequence = (
                                kv_store.get(f"session.{session_id}.agent_sequence")
                                or None
                            )

                metadata = _safe_get_metadata(request)

                observe_meta = dict(metadata.get("observe", {}))

                # Inject W3C trace context + baggage into observe_meta
                _inject_outbound_context(observe_meta)

                if traceparent:
                    observe_meta["traceparent"] = traceparent
                if session_id:
                    observe_meta["session_id"] = session_id
                    baggage.set_baggage(f"execution.{traceparent}", session_id)

                # Add agent linking info for cross-process span linking
                if last_agent_span_id:
                    observe_meta["last_agent_span_id"] = last_agent_span_id
                if last_agent_trace_id:
                    observe_meta["last_agent_trace_id"] = last_agent_trace_id
                if last_agent_name:
                    observe_meta["last_agent_name"] = last_agent_name
                if agent_sequence:
                    observe_meta["agent_sequence"] = agent_sequence

                # Add fork context for cross-process fork detection
                if session_id:
                    fork_linking_info = _get_agent_linking_info(session_id)
                    if fork_linking_info.get("fork_id"):
                        observe_meta["fork_id"] = fork_linking_info["fork_id"]
                    if fork_linking_info.get("fork_parent_seq"):
                        observe_meta["fork_parent_seq"] = fork_linking_info[
                            "fork_parent_seq"
                        ]
                    if fork_linking_info.get("fork_branch_index"):
                        observe_meta["fork_branch_index"] = fork_linking_info[
                            "fork_branch_index"
                        ]

                metadata["observe"] = observe_meta

                # Write back metadata (pydantic models are mutable by default in v2)
                try:
                    request.params.metadata = metadata
                except Exception:
                    # Fallback
                    request = request.model_copy(
                        update={
                            "params": request.params.model_copy(
                                update={"metadata": metadata}
                            )
                        }
                    )

                _emit_a2a_send_topology_event(request, self, "send_message")
            # Call through without transport-specific kwargs
            return await original_send_message(self, request, *args, **kwargs)

        legacy_client.send_message = instrumented_send_message

        # Instrument broadcast_message if it exists
        if hasattr(legacy_client, "broadcast_message"):
            original_broadcast_message = legacy_client.broadcast_message
            _original_methods[f"{legacy_module_path}.A2AClient.broadcast_message"] = (
                original_broadcast_message
            )
            original_broadcast_message = _capture_a2a_response(
                original_broadcast_message, "broadcast_message"
            )

            @functools.wraps(original_broadcast_message)
            async def instrumented_broadcast_message(self, request, *args, **kwargs):
                # Put context into A2A message metadata instead of HTTP headers
                with _global_tracer.start_as_current_span("a2a.broadcast_message"):
                    traceparent = _inject_outbound_context({}).get("traceparent")
                    session_id = _get_outbound_session_id()
                    last_agent_span_id = None
                    last_agent_trace_id = None
                    last_agent_name = None
                    agent_sequence = None
                    if traceparent:
                        if session_id:
                            kv_store.set(f"execution.{traceparent}", session_id)
                        if session_id:
                            with _kv_lock:
                                last_agent_span_id = (
                                    kv_store.get(
                                        f"session.{session_id}.last_agent_span_id"
                                    )
                                    or None
                                )
                                last_agent_trace_id = (
                                    kv_store.get(
                                        f"session.{session_id}.last_agent_trace_id"
                                    )
                                    or None
                                )
                                last_agent_name = (
                                    kv_store.get(
                                        f"session.{session_id}.last_agent_name"
                                    )
                                    or None
                                )
                                agent_sequence = (
                                    kv_store.get(f"session.{session_id}.agent_sequence")
                                    or None
                                )

                    metadata = _safe_get_metadata(request)

                    observe_meta = dict(metadata.get("observe", {}))

                    # Add agent linking info for cross-process span linking
                    if last_agent_span_id:
                        observe_meta["last_agent_span_id"] = last_agent_span_id
                    if last_agent_trace_id:
                        observe_meta["last_agent_trace_id"] = last_agent_trace_id
                    if last_agent_name:
                        observe_meta["last_agent_name"] = last_agent_name
                    if agent_sequence:
                        observe_meta["agent_sequence"] = agent_sequence

                    # Add fork context for cross-process fork detection
                    if session_id:
                        fork_linking_info = _get_agent_linking_info(session_id)
                        if fork_linking_info.get("fork_id"):
                            observe_meta["fork_id"] = fork_linking_info["fork_id"]
                        if fork_linking_info.get("fork_parent_seq"):
                            observe_meta["fork_parent_seq"] = fork_linking_info[
                                "fork_parent_seq"
                            ]
                        if fork_linking_info.get("fork_branch_index"):
                            observe_meta["fork_branch_index"] = fork_linking_info[
                                "fork_branch_index"
                            ]

                    # Inject W3C trace context + baggage into observe_meta
                    _inject_outbound_context(observe_meta)

                    if traceparent:
                        observe_meta["traceparent"] = traceparent
                    if session_id:
                        observe_meta["session_id"] = session_id
                        baggage.set_baggage(f"execution.{traceparent}", session_id)

                    metadata["observe"] = observe_meta

                    # Write back metadata (pydantic models are mutable by default in v2)
                    try:
                        request.params.metadata = metadata
                    except Exception:
                        # Fallback
                        request = request.model_copy(
                            update={
                                "params": request.params.model_copy(
                                    update={"metadata": metadata}
                                )
                            }
                        )

                    _emit_a2a_send_topology_event(request, self, "broadcast_message")
                # Call through without transport-specific kwargs
                return await original_broadcast_message(self, request, *args, **kwargs)

            legacy_client.broadcast_message = instrumented_broadcast_message

    def _instrument_server_handler(self):
        """Instrument the server-side request handlers."""
        from a2a.server.request_handlers import DefaultRequestHandler

        original_server_on_message_send = DefaultRequestHandler.on_message_send
        _original_methods["DefaultRequestHandler.on_message_send"] = (
            original_server_on_message_send
        )

        @functools.wraps(original_server_on_message_send)
        async def instrumented_on_message_send(self, params, context):
            # Read context from A2A message metadata (transport-agnostic)
            try:
                metadata = _safe_get_metadata(params)
            except Exception:
                metadata = {}

            carrier = {}
            observe_meta = metadata.get("observe", {}) or {}
            # Accept keys we inject
            for k in ("traceparent", "baggage", "session_id"):
                if k in observe_meta:
                    carrier[k] = observe_meta[k]

            token = None
            if carrier.get("traceparent"):
                # Extract and attach parent context
                ctx = TraceContextTextMapPropagator().extract(carrier=carrier)
                ctx = W3CBaggagePropagator().extract(carrier=carrier, context=ctx)
                try:
                    from opentelemetry import context as otel_ctx

                    token = otel_ctx.attach(ctx)
                except Exception:
                    token = None

                session_id = observe_meta.get("session_id")
                if session_id and session_id != "None":
                    set_session_id(session_id, traceparent=carrier.get("traceparent"))
                    kv_store.set(f"execution.{carrier.get('traceparent')}", session_id)

                    # Restore agent linking info for cross-process span linking (agent handoff event)
                    with _kv_lock:
                        last_agent_span_id = observe_meta.get("last_agent_span_id")
                        last_agent_trace_id = observe_meta.get("last_agent_trace_id")
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
                                f"session.{session_id}.last_agent_name", last_agent_name
                            )
                        if agent_sequence:
                            kv_store.set(
                                f"session.{session_id}.agent_sequence", agent_sequence
                            )

                        # Restore fork context for cross-process fork detection
                        fork_id = observe_meta.get("fork_id")
                        fork_parent_seq = observe_meta.get("fork_parent_seq")
                        fork_branch_index = observe_meta.get("fork_branch_index")
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

            try:
                _emit_a2a_receive_topology_event(params, self, "on_message_send")
                return await original_server_on_message_send(self, params, context)
            finally:
                if token is not None:
                    try:
                        from opentelemetry import context as otel_ctx

                        otel_ctx.detach(token)
                    except Exception:
                        pass

        DefaultRequestHandler.on_message_send = instrumented_on_message_send

        # Instrument on_message_send_stream for streaming message reception
        if hasattr(DefaultRequestHandler, "on_message_send_stream"):
            original_on_message_send_stream = (
                DefaultRequestHandler.on_message_send_stream
            )
            _original_methods["DefaultRequestHandler.on_message_send_stream"] = (
                original_on_message_send_stream
            )

            @functools.wraps(original_on_message_send_stream)
            async def instrumented_on_message_send_stream(self, params, context):
                # Read context from A2A message metadata (transport-agnostic)
                try:
                    metadata = _safe_get_metadata(params)
                except Exception:
                    metadata = {}

                carrier = {}
                observe_meta = metadata.get("observe", {}) or {}
                for k in ("traceparent", "baggage", "session_id"):
                    if k in observe_meta:
                        carrier[k] = observe_meta[k]

                token = None
                if carrier.get("traceparent"):
                    ctx = TraceContextTextMapPropagator().extract(carrier=carrier)
                    ctx = W3CBaggagePropagator().extract(carrier=carrier, context=ctx)
                    try:
                        from opentelemetry import context as otel_ctx

                        token = otel_ctx.attach(ctx)
                    except Exception:
                        token = None

                    session_id = observe_meta.get("session_id")
                    if session_id and session_id != "None":
                        set_session_id(
                            session_id, traceparent=carrier.get("traceparent")
                        )
                        kv_store.set(
                            f"execution.{carrier.get('traceparent')}", session_id
                        )

                        # Restore agent linking info for cross-process span linking (agent handoff event)
                        with _kv_lock:
                            last_agent_span_id = observe_meta.get("last_agent_span_id")
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
                            fork_branch_index = observe_meta.get("fork_branch_index")
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

                try:
                    _emit_a2a_receive_topology_event(
                        params, self, "on_message_send_stream"
                    )
                    # This is an async generator
                    async for event in original_on_message_send_stream(
                        self, params, context
                    ):
                        yield event
                finally:
                    if token is not None:
                        try:
                            from opentelemetry import context as otel_ctx

                            otel_ctx.detach(token)
                        except Exception:
                            pass

            DefaultRequestHandler.on_message_send_stream = (
                instrumented_on_message_send_stream
            )

    def _instrument_slima2a(self):
        """Instrument slima2a transport if available."""
        import importlib

        if importlib.util.find_spec("slima2a") is None:
            return

        from slima2a.client_transport import SRPCTransport

        # send_message
        original_srpc_transport_send_message = SRPCTransport.send_message
        _original_methods["SRPCTransport.send_message"] = (
            original_srpc_transport_send_message
        )
        original_srpc_transport_send_message = _capture_a2a_response(
            original_srpc_transport_send_message, "slima2a.send_message"
        )

        @functools.wraps(original_srpc_transport_send_message)
        async def instrumented_srpc_transport_send_message(
            self, request, *args, **kwargs
        ):
            # Put context into A2A message metadata instead of HTTP headers
            with _global_tracer.start_as_current_span("slima2a.send_message"):
                traceparent = _inject_outbound_context({}).get("traceparent")
                session_id = _get_outbound_session_id()
                last_agent_span_id = None
                last_agent_trace_id = None
                last_agent_name = None
                agent_sequence = None
                if traceparent:
                    if session_id:
                        kv_store.set(f"execution.{traceparent}", session_id)
                    # Get agent linking info for cross-process propagation
                    if session_id:
                        with _kv_lock:
                            last_agent_span_id = kv_store.get(
                                f"session.{session_id}.last_agent_span_id"
                            )
                            last_agent_trace_id = kv_store.get(
                                f"session.{session_id}.last_agent_trace_id"
                            )
                            last_agent_name = kv_store.get(
                                f"session.{session_id}.last_agent_name"
                            )
                            agent_sequence = kv_store.get(
                                f"session.{session_id}.agent_sequence"
                            )

                metadata = _safe_get_metadata(request)

                observe_meta = dict(metadata.get("observe", {}))

                # Add agent linking info for cross-process span linking
                if last_agent_span_id:
                    observe_meta["last_agent_span_id"] = last_agent_span_id
                if last_agent_trace_id:
                    observe_meta["last_agent_trace_id"] = last_agent_trace_id
                if last_agent_name:
                    observe_meta["last_agent_name"] = last_agent_name
                if agent_sequence:
                    observe_meta["agent_sequence"] = agent_sequence

                # Add fork context for cross-process fork detection
                if session_id:
                    fork_linking_info = _get_agent_linking_info(session_id)
                    if fork_linking_info.get("fork_id"):
                        observe_meta["fork_id"] = fork_linking_info["fork_id"]
                    if fork_linking_info.get("fork_parent_seq"):
                        observe_meta["fork_parent_seq"] = fork_linking_info[
                            "fork_parent_seq"
                        ]
                    if fork_linking_info.get("fork_branch_index"):
                        observe_meta["fork_branch_index"] = fork_linking_info[
                            "fork_branch_index"
                        ]

                metadata["observe"] = observe_meta

                # Inject W3C trace context + baggage into observe_meta
                _inject_outbound_context(observe_meta)

                if traceparent:
                    observe_meta["traceparent"] = traceparent
                if session_id:
                    observe_meta["session_id"] = session_id
                    baggage.set_baggage(f"execution.{traceparent}", session_id)

                metadata["observe"] = observe_meta

                request = _set_metadata(request, metadata)

                _emit_a2a_send_topology_event(request, self, "slima2a.send_message")
            # Call through without transport-specific kwargs
            return await original_srpc_transport_send_message(
                self, request, *args, **kwargs
            )

        SRPCTransport.send_message = instrumented_srpc_transport_send_message

        # send_message_streaming
        original_srpc_transport_send_message_streaming = (
            SRPCTransport.send_message_streaming
        )
        _original_methods["SRPCTransport.send_message_streaming"] = (
            original_srpc_transport_send_message_streaming
        )
        original_srpc_transport_send_message_streaming = _capture_a2a_response(
            original_srpc_transport_send_message_streaming,
            "slima2a.send_message_streaming",
            streaming=True,
        )

        @functools.wraps(original_srpc_transport_send_message_streaming)
        async def instrumented_srpc_transport_send_message_streaming(
            self, request, *args, **kwargs
        ):
            # Put context into A2A message metadata instead of HTTP headers
            with _global_tracer.start_as_current_span("slima2a.send_message_streaming"):
                traceparent = _inject_outbound_context({}).get("traceparent")
                session_id = _get_outbound_session_id()
                last_agent_span_id = None
                last_agent_trace_id = None
                last_agent_name = None
                agent_sequence = None
                if traceparent:
                    if session_id:
                        kv_store.set(f"execution.{traceparent}", session_id)
                    # Get agent linking info for cross-process propagation
                    if session_id:
                        with _kv_lock:
                            last_agent_span_id = kv_store.get(
                                f"session.{session_id}.last_agent_span_id"
                            )
                            last_agent_trace_id = kv_store.get(
                                f"session.{session_id}.last_agent_trace_id"
                            )
                            last_agent_name = kv_store.get(
                                f"session.{session_id}.last_agent_name"
                            )
                            agent_sequence = kv_store.get(
                                f"session.{session_id}.agent_sequence"
                            )

                metadata = _safe_get_metadata(request)
                observe_meta = dict(metadata.get("observe", {}))

                # Add agent linking info for cross-process span linking
                if last_agent_span_id:
                    observe_meta["last_agent_span_id"] = last_agent_span_id
                if last_agent_trace_id:
                    observe_meta["last_agent_trace_id"] = last_agent_trace_id
                if last_agent_name:
                    observe_meta["last_agent_name"] = last_agent_name
                if agent_sequence:
                    observe_meta["agent_sequence"] = agent_sequence

                # Add fork context for cross-process fork detection
                if session_id:
                    fork_linking_info = _get_agent_linking_info(session_id)
                    if fork_linking_info.get("fork_id"):
                        observe_meta["fork_id"] = fork_linking_info["fork_id"]
                    if fork_linking_info.get("fork_parent_seq"):
                        observe_meta["fork_parent_seq"] = fork_linking_info[
                            "fork_parent_seq"
                        ]
                    if fork_linking_info.get("fork_branch_index"):
                        observe_meta["fork_branch_index"] = fork_linking_info[
                            "fork_branch_index"
                        ]

                metadata["observe"] = observe_meta

                # Inject W3C trace context + baggage into observe_meta
                _inject_outbound_context(observe_meta)

                if traceparent:
                    observe_meta["traceparent"] = traceparent
                if session_id:
                    observe_meta["session_id"] = session_id
                    baggage.set_baggage(f"execution.{traceparent}", session_id)

                metadata["observe"] = observe_meta

                request = _set_metadata(request, metadata)

                _emit_a2a_send_topology_event(
                    request, self, "slima2a.send_message_streaming"
                )
            async for response in original_srpc_transport_send_message_streaming(
                self, request, *args, **kwargs
            ):
                yield response

        SRPCTransport.send_message_streaming = (
            instrumented_srpc_transport_send_message_streaming
        )

    def _uninstrument(self, **kwargs):
        import importlib

        for cls, method, original, owned in reversed(
            getattr(self, "_card_methods", [])
        ):
            if owned:
                setattr(cls, method, original)
            else:
                delattr(cls, method)
        self._card_methods = []

        if importlib.util.find_spec("a2a") is None:
            raise ImportError("No module named 'a2a-sdk'. Please install it first.")

        # Uninstrument BaseA2AClient (new pattern v0.3.0+)
        if importlib.util.find_spec("a2a.client.base_client") is not None:
            try:
                from a2a.client.base_client import BaseClient as BaseA2AClient

                if "BaseA2AClient.send_message" in _original_methods:
                    BaseA2AClient.send_message = _original_methods[
                        "BaseA2AClient.send_message"
                    ]
                elif hasattr(BaseA2AClient.send_message, "__wrapped__"):
                    BaseA2AClient.send_message = BaseA2AClient.send_message.__wrapped__

                if "BaseA2AClient.send_message_streaming" in _original_methods:
                    BaseA2AClient.send_message_streaming = _original_methods[
                        "BaseA2AClient.send_message_streaming"
                    ]
                elif hasattr(BaseA2AClient, "send_message_streaming") and hasattr(
                    BaseA2AClient.send_message_streaming, "__wrapped__"
                ):
                    BaseA2AClient.send_message_streaming = (
                        BaseA2AClient.send_message_streaming.__wrapped__
                    )
            except ImportError:
                pass

        # Uninstrument legacy A2AClient
        legacy_client = None
        legacy_module_path = None

        try:
            from a2a.client.legacy import A2AClient as LegacyA2AClient

            legacy_client = LegacyA2AClient
            legacy_module_path = "a2a.client.legacy"
        except ImportError:
            try:
                from a2a.client import A2AClient as LegacyA2AClient

                if hasattr(LegacyA2AClient, "send_message"):
                    legacy_client = LegacyA2AClient
                    legacy_module_path = "a2a.client"
            except ImportError:
                pass

        if legacy_client is not None:
            key = f"{legacy_module_path}.A2AClient.send_message"
            if key in _original_methods:
                legacy_client.send_message = _original_methods[key]
            elif hasattr(legacy_client.send_message, "__wrapped__"):
                legacy_client.send_message = legacy_client.send_message.__wrapped__

            broadcast_key = f"{legacy_module_path}.A2AClient.broadcast_message"
            if broadcast_key in _original_methods:
                legacy_client.broadcast_message = _original_methods[broadcast_key]
            elif hasattr(legacy_client, "broadcast_message") and hasattr(
                legacy_client.broadcast_message, "__wrapped__"
            ):
                legacy_client.broadcast_message = (
                    legacy_client.broadcast_message.__wrapped__
                )

        # Uninstrument server handler
        from a2a.server.request_handlers import DefaultRequestHandler

        if "DefaultRequestHandler.on_message_send" in _original_methods:
            DefaultRequestHandler.on_message_send = _original_methods[
                "DefaultRequestHandler.on_message_send"
            ]
        elif hasattr(DefaultRequestHandler.on_message_send, "__wrapped__"):
            DefaultRequestHandler.on_message_send = (
                DefaultRequestHandler.on_message_send.__wrapped__
            )

        if "DefaultRequestHandler.on_message_send_stream" in _original_methods:
            DefaultRequestHandler.on_message_send_stream = _original_methods[
                "DefaultRequestHandler.on_message_send_stream"
            ]
        elif hasattr(DefaultRequestHandler, "on_message_send_stream") and hasattr(
            DefaultRequestHandler.on_message_send_stream, "__wrapped__"
        ):
            DefaultRequestHandler.on_message_send_stream = (
                DefaultRequestHandler.on_message_send_stream.__wrapped__
            )

        # Uninstrument slima2a
        if importlib.util.find_spec("slima2a"):
            from slima2a.client_transport import SRPCTransport

            if "SRPCTransport.send_message" in _original_methods:
                SRPCTransport.send_message = _original_methods[
                    "SRPCTransport.send_message"
                ]
            elif hasattr(SRPCTransport, "send_message") and hasattr(
                SRPCTransport.send_message, "__wrapped__"
            ):
                SRPCTransport.send_message = SRPCTransport.send_message.__wrapped__

            if "SRPCTransport.send_message_streaming" in _original_methods:
                SRPCTransport.send_message_streaming = _original_methods[
                    "SRPCTransport.send_message_streaming"
                ]
            elif hasattr(SRPCTransport, "send_message_streaming") and hasattr(
                SRPCTransport.send_message_streaming, "__wrapped__"
            ):
                SRPCTransport.send_message_streaming = (
                    SRPCTransport.send_message_streaming.__wrapped__
                )

        # Clear stored original methods
        _original_methods.clear()
