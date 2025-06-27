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
from ioa_observe.sdk.tracing import set_execution_id, get_current_traceparent

_instruments = ("python-a2a >= 0.2.5",)
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
        try:
            import a2a
        except ImportError:
            raise ImportError("No module named 'a2a'. Please install it first.")

        # Instrument `publish`
        original_send_message = a2a.client.A2AClient.send_message

        @functools.wraps(original_send_message)
        async def instrumented_send_message(
            self, request, *, http_kwargs=None, context=None
        ):
            with _global_tracer.start_as_current_span("a2a.send_message"):
                traceparent = get_current_traceparent()
                execution_id = None
                if traceparent:
                    execution_id = kv_store.get(f"execution.{traceparent}")
                    if execution_id:
                        kv_store.set(f"execution.{traceparent}", execution_id)
                # Inject headers into http_kwargs
                if http_kwargs is None:
                    http_kwargs = {}
                headers = http_kwargs.get("headers", {})
                headers["traceparent"] = traceparent
                if execution_id:
                    headers["execution_id"] = execution_id
                    baggage.set_baggage(f"execution.{traceparent}", execution_id)
                http_kwargs["headers"] = headers
            return await original_send_message(
                self, request, http_kwargs=http_kwargs, context=context
            )

        a2a.client.A2AClient.send_message = instrumented_send_message

        original_execute = a2a.server.agent_execution.AgentExecutor.execute

        @functools.wraps(original_execute)
        async def instrumented_execute(self, context, event_queue):
            # Extract headers from context (assume context.request.headers)
            headers = getattr(getattr(context, "request", None), "headers", {})
            traceparent = headers.get("traceparent")
            execution_id = headers.get("execution_id")
            carrier = {
                k.lower(): v
                for k, v in headers.items()
                if k.lower() in ["traceparent", "baggage"]
            }
            if carrier and traceparent:
                ctx = TraceContextTextMapPropagator().extract(carrier=carrier)
                ctx = W3CBaggagePropagator().extract(carrier=carrier, context=ctx)
                if execution_id and execution_id != "None":
                    set_execution_id(execution_id, traceparent=traceparent)
                    kv_store.set(f"execution.{traceparent}", execution_id)
            return await original_execute(self, context, event_queue)

        a2a.server.agent_execution.AgentExecutor.execute = instrumented_execute

    def _uninstrument(self, **kwargs):
        try:
            import a2a
        except ImportError:
            raise ImportError("No module named 'a2a'. Please install it first.")

        # Uninstrument `send_message`
        a2a.client.A2AClient.send_message = (
            a2a.client.A2AClient.send_message.__wrapped__
        )

        # Uninstrument `execute`
        a2a.server.agent_execution.AgentExecutor.execute = (
            a2a.server.agent_execution.AgentExecutor.execute.__wrapped__
        )
