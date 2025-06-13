from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry import baggage

from ioa_observe.sdk import TracerWrapper
from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.tracing import set_execution_id, get_current_traceparent
from opentelemetry import context as otel_context

"""
Usage Example:


Sender:
Before sending a message, call get_current_context_headers() and attach the returned headers to your message.
Receiver:
After receiving a message, extract headers and call set_context_from_headers(headers) before processing.
"""

def get_current_context_headers():
    """
    Extracts the current trace context, baggage, and execution_id into headers.
    """
    _global_tracer = TracerWrapper().get_tracer()
    with _global_tracer.start_as_current_span("get_current_context_headers"):
        carrier = {}
        # Use the current OpenTelemetry context for injection
        current_ctx = otel_context.get_current()
        TraceContextTextMapPropagator().inject(carrier, context=current_ctx)
        W3CBaggagePropagator().inject(carrier, context=current_ctx)
        traceparent = carrier.get("traceparent")
        execution_id = None
        if traceparent:
            execution_id = kv_store.get(f"execution.{traceparent}")
            if execution_id:
                carrier["execution_id"] = execution_id
        return carrier

def set_context_from_headers(headers):
    """
    Restores the trace context, baggage, and execution_id from headers.
    """
    # Extract trace context and baggage
    _global_tracer = TracerWrapper().get_tracer()
    with _global_tracer.start_as_current_span("set_context_from_headers"):
        ctx = TraceContextTextMapPropagator().extract(carrier=headers)
        ctx = W3CBaggagePropagator().extract(carrier=headers, context=ctx)
        # Restore execution_id if present
        if headers is not None:
            if "traceparent" in headers:
                traceparent = headers.get("traceparent")
            if "executionID" in headers:
                execution_id = headers.get("executionID")
        if traceparent and execution_id and execution_id != "None":
            set_execution_id(execution_id, traceparent=traceparent)
            kv_store.set(f"execution.{traceparent}", execution_id)
        return ctx

def set_baggage_item(key, value):
    baggage.set_baggage(key, value)

def get_baggage_item(key):
    return baggage.get_baggage(key)

def get_current_execution_id():
    traceparent = get_current_traceparent()
    if traceparent:
        return kv_store.get(f"execution.{traceparent}")
    return None

def set_execution_id_from_headers(headers):
    traceparent = headers.get("traceparent")
    execution_id = headers.get("execution_id")
    if traceparent and execution_id:
        set_execution_id(execution_id, traceparent=traceparent)
        kv_store.set(f"execution.{traceparent}", execution_id)