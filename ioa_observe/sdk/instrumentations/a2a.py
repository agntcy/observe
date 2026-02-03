# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from typing import Collection
import functools
import threading

from opentelemetry.context import get_value
from opentelemetry import baggage
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from ioa_observe.sdk import TracerWrapper
from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.tracing import set_session_id, get_current_traceparent

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
            with _global_tracer.start_as_current_span("a2a.send_message"):
                traceparent = get_current_traceparent()
                session_id = None
                last_agent_span_id = None
                last_agent_trace_id = None
                last_agent_name = None
                agent_sequence = None
                if traceparent:
                    session_id = kv_store.get(f"execution.{traceparent}")
                    if not session_id:
                        session_id = get_value("session.id")
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

                # Add agent linking info for cross-process span linking
                if last_agent_span_id:
                    observe_meta["last_agent_span_id"] = last_agent_span_id
                if last_agent_trace_id:
                    observe_meta["last_agent_trace_id"] = last_agent_trace_id
                if last_agent_name:
                    observe_meta["last_agent_name"] = last_agent_name
                if agent_sequence:
                    observe_meta["agent_sequence"] = agent_sequence

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
            return await original_send_message(self, request, *args, **kwargs)

        A2AClient.send_message = instrumented_send_message

        # Instrument broadcast_message
        if hasattr(A2AClient, "broadcast_message"):
            original_broadcast_message = A2AClient.broadcast_message

            @functools.wraps(original_broadcast_message)
            async def instrumented_broadcast_message(self, request, *args, **kwargs):
                # Put context into A2A message metadata instead of HTTP headers
                with _global_tracer.start_as_current_span("a2a.broadcast_message"):
                    traceparent = get_current_traceparent()
                    session_id = None
                    if traceparent:
                        session_id = kv_store.get(f"execution.{traceparent}")
                        if not session_id:
                            session_id = get_value("session.id")
                            if session_id:
                                kv_store.set(f"execution.{traceparent}", session_id)

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
                return await original_broadcast_message(self, request, *args, **kwargs)

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

        # Instrumentation for slima2a
        if importlib.util.find_spec("slima2a"):
            from slima2a.client_transport import SRPCTransport

            # send_message
            original_srpc_transport_send_message = SRPCTransport.send_message

            @functools.wraps(original_srpc_transport_send_message)
            async def instrumented_srpc_transport_send_message(
                self, request, *args, **kwargs
            ):
                # Put context into A2A message metadata instead of HTTP headers
                with _global_tracer.start_as_current_span("slima2a.send_message"):
                    traceparent = get_current_traceparent()
                    session_id = None
                    last_agent_span_id = None
                    last_agent_trace_id = None
                    last_agent_name = None
                    agent_sequence = None
                    if traceparent:
                        session_id = kv_store.get(f"execution.{traceparent}")
                        if not session_id:
                            session_id = get_value("session.id")
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

                    # Add agent linking info for cross-process span linking
                    if last_agent_span_id:
                        observe_meta["last_agent_span_id"] = last_agent_span_id
                    if last_agent_trace_id:
                        observe_meta["last_agent_trace_id"] = last_agent_trace_id
                    if last_agent_name:
                        observe_meta["last_agent_name"] = last_agent_name
                    if agent_sequence:
                        observe_meta["agent_sequence"] = agent_sequence

                    metadata["observe"] = observe_meta

                    # Write back metadata (pydantic models are mutable by default in v2)
                    try:
                        request.metadata = metadata
                    except Exception:
                        # Fallback
                        request = request.model_copy(update={"metadata": metadata})

                # Call through without transport-specific kwargs
                return await original_srpc_transport_send_message(
                    self, request, *args, **kwargs
                )

            SRPCTransport.send_message = instrumented_srpc_transport_send_message

            # send_message_streaming
            original_srpc_transport_send_message_streaming = (
                SRPCTransport.send_message_streaming
            )

            @functools.wraps(original_srpc_transport_send_message_streaming)
            async def instrumented_srpc_transport_send_message_streaming(
                self, request, *args, **kwargs
            ):
                # Put context into A2A message metadata instead of HTTP headers
                with _global_tracer.start_as_current_span(
                    "slima2a.send_message_streaming"
                ):
                    traceparent = get_current_traceparent()
                    session_id = None
                    last_agent_span_id = None
                    last_agent_trace_id = None
                    last_agent_name = None
                    agent_sequence = None
                    if traceparent:
                        session_id = kv_store.get(f"execution.{traceparent}")
                        if not session_id:
                            session_id = get_value("session.id")
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

                    # Add agent linking info for cross-process span linking
                    if last_agent_span_id:
                        observe_meta["last_agent_span_id"] = last_agent_span_id
                    if last_agent_trace_id:
                        observe_meta["last_agent_trace_id"] = last_agent_trace_id
                    if last_agent_name:
                        observe_meta["last_agent_name"] = last_agent_name
                    if agent_sequence:
                        observe_meta["agent_sequence"] = agent_sequence

                    metadata["observe"] = observe_meta

                    # Write back metadata (pydantic models are mutable by default in v2)
                    try:
                        request.metadata = metadata
                    except Exception:
                        # Fallback
                        request = request.model_copy(update={"metadata": metadata})

                # Call through without transport-specific kwargs
                return await original_srpc_transport_send_message_streaming(
                    self, request, *args, **kwargs
                )

            SRPCTransport.send_message_streaming = (
                instrumented_srpc_transport_send_message_streaming
            )

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

        # handle slima2a
        if importlib.util.find_spec("slima2a"):
            from slima2a.client_transport import SRPCTransport

            # Uninstrument `send_message`
            if hasattr(SRPCTransport, "send_message") and hasattr(
                SRPCTransport.send_message, "__wrapped__"
            ):
                SRPCTransport.send_message = SRPCTransport.send_message.__wrapped__

            if hasattr(SRPCTransport, "send_message_streaming") and hasattr(
                SRPCTransport.send_message_streaming, "__wrapped__"
            ):
                SRPCTransport.send_message_streaming = (
                    SRPCTransport.send_message.__wrapped__
                )
