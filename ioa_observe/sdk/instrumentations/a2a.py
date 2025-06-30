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

_instruments = ("a2a-sdk >= 0.2.5",)
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

        # Instrument `publish`
        from a2a.client import A2AClient

        original_send_message = A2AClient.send_message

        @functools.wraps(original_send_message)
        async def instrumented_send_message(
            self, request, *, http_kwargs=None, context=None
        ):
            with _global_tracer.start_as_current_span("a2a.send_message"):
                traceparent = get_current_traceparent()
                session_id = None
                if traceparent:
                    session_id = kv_store.get(f"execution.{traceparent}")
                    if session_id:
                        kv_store.set(f"execution.{traceparent}", session_id)
                # Inject headers into http_kwargs
                if http_kwargs is None:
                    http_kwargs = {}
                headers = http_kwargs.get("headers", {})
                headers["traceparent"] = traceparent
                if session_id:
                    headers["session_id"] = session_id
                    baggage.set_baggage(f"execution.{traceparent}", session_id)
                http_kwargs["headers"] = headers
            return await original_send_message(self, request, http_kwargs=http_kwargs)

        from a2a.client import A2AClient

        A2AClient.send_message = instrumented_send_message

        from a2a.server.request_handlers import DefaultRequestHandler

        original_server_on_message_send = DefaultRequestHandler.on_message_send

        @functools.wraps(original_server_on_message_send)
        async def instrumented_execute(self, params, context):
            # Extract headers from context (assume context.request.headers)

            traceparent = context.state.get("headers", {}).get("traceparent")
            session_id = context.state.get("headers", {}).get("session_id")
            carrier = {
                k.lower(): v
                for k, v in context.state.get("headers", {}).items()
                if k.lower() in ["traceparent", "baggage"]
            }
            if carrier and traceparent:
                ctx = TraceContextTextMapPropagator().extract(carrier=carrier)
                ctx = W3CBaggagePropagator().extract(carrier=carrier, context=ctx)
                if session_id and session_id != "None":
                    set_session_id(session_id, traceparent=traceparent)
                    kv_store.set(f"execution.{traceparent}", session_id)
            return await original_server_on_message_send(self, params, context)

        from a2a.server.request_handlers import DefaultRequestHandler

        DefaultRequestHandler.on_message_send = instrumented_execute

    def _uninstrument(self, **kwargs):
        import importlib

        if importlib.util.find_spec("a2a") is None:
            raise ImportError("No module named 'a2a-sdk'. Please install it first.")

        # Uninstrument `send_message`
        from a2a.client import A2AClient

        A2AClient.send_message = A2AClient.send_message.__wrapped__

        # Uninstrument `execute`
        from a2a.server.request_handlers import DefaultRequestHandler

        DefaultRequestHandler.on_message_send = (
            DefaultRequestHandler.on_message_send.__wrapped__
        )
