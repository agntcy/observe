# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from typing import Collection
import functools
import threading

from opentelemetry import baggage
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from ioa_observe.sdk import TracerWrapper
from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.tracing import set_session_id, get_current_traceparent
from ioa_observe.sdk.semantic_conventions import (
    AgentCommunicationAttributes,
    AgentCommunicationEvents,
    StatusCode,
    set_communication_attributes,
    record_message_event,
    set_error_attributes,
)

_instruments = ("a2a-sdk >= 0.3.0",)
_global_tracer = None
_kv_lock = threading.RLock()  # Add thread-safety for kv_store operations


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

        # Instrument client send_message
        from a2a.client import A2AClient

        original_send_message = A2AClient.send_message

        @functools.wraps(original_send_message)
        async def instrumented_send_message(self, request, *args, **kwargs):
            # Put context into A2A message metadata instead of HTTP headers
            with _global_tracer.start_as_current_span("gen_ai.agent.communication") as span:
                traceparent = get_current_traceparent()
                session_id = None
                if traceparent:
                    session_id = kv_store.get(f"execution.{traceparent}")
                    if session_id:
                        kv_store.set(f"execution.{traceparent}", session_id)

                # Extract message and receiver info for semantic conventions
                message = None
                receiver_id = None
                message_id = None

                # Try to extract message content
                try:
                    if hasattr(request, "params"):
                        message = getattr(request.params, "message", None)
                        # Try to get receiver from target or other fields
                        receiver_id = getattr(request.params, "target", None)
                        message_id = getattr(request.params, "message_id", None) or getattr(request, "id", None)
                except Exception:
                    pass

                # Set standardized semantic conventions
                set_communication_attributes(
                    span=span,
                    protocol="a2a",
                    receiver_id=receiver_id,
                    message=message,
                    session_id=session_id,
                    message_id=message_id,
                    operation="send_message",
                )

                # Record message sent event
                record_message_event(
                    span,
                    AgentCommunicationEvents.MESSAGE_SENT,
                    message=message,
                )

                # Ensure metadata dict exists
                try:
                    md = getattr(request.params, "metadata", None)
                except AttributeError:
                    md = None
                metadata = md if isinstance(md, dict) else {}

                observe_meta = dict(metadata.get("observe", {}))

                # Inject W3C trace context + baggage into observe_meta
                TraceContextTextMapPropagator().inject(carrier=observe_meta)
                W3CBaggagePropagator().inject(carrier=observe_meta)

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

            # Call through without transport-specific kwargs
            try:
                result = await original_send_message(self, request, *args, **kwargs)
                # Set success status
                from opentelemetry import trace
                current_span = trace.get_current_span()
                if current_span and current_span.is_recording():
                    current_span.set_attribute(
                        AgentCommunicationAttributes.STATUS_CODE,
                        StatusCode.OK
                    )
                return result
            except Exception as e:
                # Record error in span
                from opentelemetry import trace
                current_span = trace.get_current_span()
                if current_span and current_span.is_recording():
                    set_error_attributes(current_span, e)
                raise

        A2AClient.send_message = instrumented_send_message

        # Instrument broadcast_message
        if hasattr(A2AClient, "broadcast_message"):
            original_broadcast_message = A2AClient.broadcast_message

            @functools.wraps(original_broadcast_message)
            async def instrumented_broadcast_message(self, request, *args, **kwargs):
                # Put context into A2A message metadata instead of HTTP headers
                with _global_tracer.start_as_current_span("gen_ai.agent.communication") as span:
                    traceparent = get_current_traceparent()
                    session_id = None
                    if traceparent:
                        session_id = kv_store.get(f"execution.{traceparent}")
                        if session_id:
                            kv_store.set(f"execution.{traceparent}", session_id)

                    # Extract message info for semantic conventions
                    message = None
                    message_id = None

                    try:
                        if hasattr(request, "params"):
                            message = getattr(request.params, "message", None)
                            message_id = getattr(request.params, "message_id", None) or getattr(request, "id", None)
                    except Exception:
                        pass

                    # Set standardized semantic conventions (broadcast has no specific receiver)
                    set_communication_attributes(
                        span=span,
                        protocol="a2a",
                        message=message,
                        session_id=session_id,
                        message_id=message_id,
                        operation="broadcast_message",
                    )

                    # Mark as broadcast type
                    span.set_attribute(AgentCommunicationAttributes.MESSAGE_TYPE, "broadcast")

                    # Record message sent event
                    record_message_event(
                        span,
                        AgentCommunicationEvents.MESSAGE_SENT,
                        message=message,
                    )

                    # Ensure metadata dict exists
                    try:
                        md = getattr(request.params, "metadata", None)
                    except AttributeError:
                        md = None
                    metadata = md if isinstance(md, dict) else {}

                    observe_meta = dict(metadata.get("observe", {}))

                    # Inject W3C trace context + baggage into observe_meta
                    TraceContextTextMapPropagator().inject(carrier=observe_meta)
                    W3CBaggagePropagator().inject(carrier=observe_meta)

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

                # Call through without transport-specific kwargs
                try:
                    result = await original_broadcast_message(self, request, *args, **kwargs)
                    # Set success status
                    from opentelemetry import trace
                    current_span = trace.get_current_span()
                    if current_span and current_span.is_recording():
                        current_span.set_attribute(
                            AgentCommunicationAttributes.STATUS_CODE,
                            StatusCode.OK
                        )
                    return result
                except Exception as e:
                    # Record error in span
                    from opentelemetry import trace
                    current_span = trace.get_current_span()
                    if current_span and current_span.is_recording():
                        set_error_attributes(current_span, e)
                    raise

            A2AClient.broadcast_message = instrumented_broadcast_message

        # Instrument server handler
        from a2a.server.request_handlers import DefaultRequestHandler

        original_server_on_message_send = DefaultRequestHandler.on_message_send

        @functools.wraps(original_server_on_message_send)
        async def instrumented_on_message_send(self, params, context):
            # Read context from A2A message metadata (transport-agnostic)
            try:
                metadata = getattr(params, "metadata", {}) or {}
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

                # Record message received event
                if _global_tracer:
                    from opentelemetry import trace
                    current_span = trace.get_current_span()
                    if current_span and current_span.is_recording():
                        # Extract message for the event
                        message = None
                        try:
                            message = getattr(params, "message", None)
                        except Exception:
                            pass

                        record_message_event(
                            current_span,
                            AgentCommunicationEvents.MESSAGE_RECEIVED,
                            message=message,
                        )

            try:
                return await original_server_on_message_send(self, params, context)
            finally:
                if token is not None:
                    try:
                        otel_ctx.detach(token)
                    except Exception:
                        pass

        DefaultRequestHandler.on_message_send = instrumented_on_message_send

        # from a2a.client import A2AClient

        # A2AClient.send_message = instrumented_send_message

        # from a2a.server.request_handlers import DefaultRequestHandler

        # original_server_on_message_send = DefaultRequestHandler.on_message_send

    def _uninstrument(self, **kwargs):
        import importlib

        if importlib.util.find_spec("a2a") is None:
            raise ImportError("No module named 'a2a-sdk'. Please install it first.")

        # Uninstrument `send_message`
        from a2a.client import A2AClient

        A2AClient.send_message = A2AClient.send_message.__wrapped__

        # Uninstrument `broadcast_message`
        if hasattr(A2AClient, "broadcast_message") and hasattr(
            A2AClient.broadcast_message, "__wrapped__"
        ):
            A2AClient.broadcast_message = A2AClient.broadcast_message.__wrapped__

        # Uninstrument server handler
        from a2a.server.request_handlers import DefaultRequestHandler

        DefaultRequestHandler.on_message_send = (
            DefaultRequestHandler.on_message_send.__wrapped__
        )
