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
from ioa_observe.sdk.tracing.context_utils import _get_agent_linking_info

_instruments = ("a2a-sdk >= 0.3.0",)
_global_tracer = None
_kv_lock = threading.RLock()  # Add thread-safety for kv_store operations

# Track original methods for uninstrumentation
_original_methods = {}


def _inject_observe_metadata(request, span_name: str):
    """
    Helper function to inject observability metadata into request.
    This is shared between legacy and new client instrumentation.
    """
    traceparent = get_current_traceparent()
    session_id = None
    if traceparent:
        session_id = kv_store.get(f"execution.{traceparent}")
        if not session_id:
            session_id = get_value("session.id")
            if session_id:
                kv_store.set(f"execution.{traceparent}", session_id)

    # Get agent linking info for cross-process propagation (agent handoff event)
    agent_linking_info = _get_agent_linking_info(session_id) if session_id else {}

    # Ensure metadata dict exists - handle both request.params.metadata and request.metadata
    try:
        if hasattr(request, "params") and hasattr(request.params, "metadata"):
            md = getattr(request.params, "metadata", None)
        else:
            md = getattr(request, "metadata", None)
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

    # Write back metadata (pydantic models are mutable by default in v2)
    try:
        if hasattr(request, "params") and hasattr(request.params, "metadata"):
            request.params.metadata = metadata
        else:
            request.metadata = metadata
    except Exception:
        # Fallback - create a new request with updated metadata
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
            pass  # If all else fails, continue without metadata injection

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

        # Instrument slima2a if available
        self._instrument_slima2a()

    def _instrument_base_client(self):
        """Instrument the new A2AClient (BaseClient) pattern introduced in v0.3.0."""
        import importlib

        # Check if the new base_client module exists
        if importlib.util.find_spec("a2a.client.base_client") is None:
            return

        try:
            from a2a.client.base_client import A2AClient as BaseA2AClient
        except ImportError:
            return

        # Instrument send_message
        if hasattr(BaseA2AClient, "send_message"):
            original_send_message = BaseA2AClient.send_message
            _original_methods["BaseA2AClient.send_message"] = original_send_message

            @functools.wraps(original_send_message)
            async def instrumented_send_message(self, request, *args, **kwargs):
                nonlocal original_send_message
                with _global_tracer.start_as_current_span("a2a.client.send_message"):
                    request = _inject_observe_metadata(
                        request, "a2a.client.send_message"
                    )
                return await original_send_message(self, request, *args, **kwargs)

            BaseA2AClient.send_message = instrumented_send_message

        # Instrument send_message_streaming
        if hasattr(BaseA2AClient, "send_message_streaming"):
            original_send_message_streaming = BaseA2AClient.send_message_streaming
            _original_methods["BaseA2AClient.send_message_streaming"] = (
                original_send_message_streaming
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

                # Add fork context for cross-process fork detection
                if session_id:
                    fork_linking_info = _get_agent_linking_info(session_id)
                    if fork_linking_info.get("fork_id"):
                        observe_meta["fork_id"] = fork_linking_info["fork_id"]
                    if fork_linking_info.get("fork_parent_seq"):
                        observe_meta["fork_parent_seq"] = fork_linking_info["fork_parent_seq"]
                    if fork_linking_info.get("fork_branch_index"):
                        observe_meta["fork_branch_index"] = fork_linking_info["fork_branch_index"]

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

        legacy_client.send_message = instrumented_send_message

        # Instrument broadcast_message if it exists
        if hasattr(legacy_client, "broadcast_message"):
            original_broadcast_message = legacy_client.broadcast_message
            _original_methods[f"{legacy_module_path}.A2AClient.broadcast_message"] = (
                original_broadcast_message
            )

            @functools.wraps(original_broadcast_message)
            async def instrumented_broadcast_message(self, request, *args, **kwargs):
                # Put context into A2A message metadata instead of HTTP headers
                with _global_tracer.start_as_current_span("a2a.broadcast_message"):
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

                    # Ensure metadata dict exists
                    try:
                        md = getattr(request.params, "metadata", None)
                    except AttributeError:
                        md = None
                    metadata = md if isinstance(md, dict) else {}

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
                            observe_meta["fork_parent_seq"] = fork_linking_info["fork_parent_seq"]
                        if fork_linking_info.get("fork_branch_index"):
                            observe_meta["fork_branch_index"] = fork_linking_info["fork_branch_index"]

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
                    metadata = getattr(params, "metadata", {}) or {}
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
                        observe_meta["fork_parent_seq"] = fork_linking_info["fork_parent_seq"]
                    if fork_linking_info.get("fork_branch_index"):
                        observe_meta["fork_branch_index"] = fork_linking_info["fork_branch_index"]

                metadata["observe"] = observe_meta

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
        _original_methods["SRPCTransport.send_message_streaming"] = (
            original_srpc_transport_send_message_streaming
        )

        @functools.wraps(original_srpc_transport_send_message_streaming)
        async def instrumented_srpc_transport_send_message_streaming(
            self, request, *args, **kwargs
        ):
            # Put context into A2A message metadata instead of HTTP headers
            with _global_tracer.start_as_current_span("slima2a.send_message_streaming"):
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
                        observe_meta["fork_parent_seq"] = fork_linking_info["fork_parent_seq"]
                    if fork_linking_info.get("fork_branch_index"):
                        observe_meta["fork_branch_index"] = fork_linking_info["fork_branch_index"]

                metadata["observe"] = observe_meta

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

        # Uninstrument BaseA2AClient (new pattern v0.3.0+)
        if importlib.util.find_spec("a2a.client.base_client") is not None:
            try:
                from a2a.client.base_client import A2AClient as BaseA2AClient

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
