import threading
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry import baggage

from ioa_observe.sdk import TracerWrapper
from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.tracing import set_session_id, get_current_traceparent
from opentelemetry import context as otel_context
from opentelemetry.context import attach

"""
Usage Example:


Sender:
Before sending a message, call get_current_context_headers() and attach the returned headers to your message.
Receiver:
After receiving a message, extract headers and call set_context_from_headers(headers) before processing.
"""

_kv_lock = threading.RLock()


def _get_agent_linking_info(session_id):
    """Get agent linking info for cross-process propagation.

    Returns:
        dict: Agent linking info including span_id, trace_id, agent_name, and sequence.
    """
    if not session_id:
        return {}

    linking_info = {}
    with _kv_lock:
        last_agent_span_id = kv_store.get(f"session.{session_id}.last_agent_span_id")
        last_agent_trace_id = kv_store.get(f"session.{session_id}.last_agent_trace_id")
        last_agent_name = kv_store.get(f"session.{session_id}.last_agent_name")
        agent_sequence = kv_store.get(f"session.{session_id}.agent_sequence")

        if last_agent_span_id:
            linking_info["last_agent_span_id"] = last_agent_span_id
        if last_agent_trace_id:
            linking_info["last_agent_trace_id"] = last_agent_trace_id
        if last_agent_name:
            linking_info["last_agent_name"] = last_agent_name
        if agent_sequence:
            linking_info["agent_sequence"] = agent_sequence

    return linking_info


def get_current_context_headers():
    """
    Extracts the current trace context, baggage, and session_id into headers.
    """
    _global_tracer = TracerWrapper().get_tracer()
    with _global_tracer.start_as_current_span("get_current_context_headers"):
        carrier = {}
        # Use the current OpenTelemetry context for injection
        current_ctx = otel_context.get_current()
        TraceContextTextMapPropagator().inject(carrier, context=current_ctx)
        W3CBaggagePropagator().inject(carrier, context=current_ctx)
        traceparent = carrier.get("traceparent")
        session_id = None
        if traceparent:
            session_id = kv_store.get(f"execution.{traceparent}")
            if session_id:
                carrier["session_id"] = session_id
        if session_id:
            linking_info = _get_agent_linking_info(session_id)
            carrier.update(linking_info)
        return carrier


def set_context_from_headers(headers):
    """
    Restores the trace context, baggage, and session_id from headers.
    """
    carrierHeaders = {}
    if "traceparentID" in headers:
        carrierHeaders["traceparent"] = headers["traceparentID"]
    if "executionID" in headers:
        carrierHeaders["session_id"] = headers["executionID"]
    ctx = TraceContextTextMapPropagator().extract(carrier=carrierHeaders)
    ctx = W3CBaggagePropagator().extract(carrier=carrierHeaders, context=ctx)
    attach(ctx)
    # Restore session_id if present
    traceparent = headers.get("traceparentID")
    session_id = headers.get("executionID")
    if traceparent and session_id and session_id != "None":
        set_session_id(session_id, traceparent=traceparent)
        kv_store.set(f"execution.{traceparent}", session_id)
    return ctx


def set_baggage_item(key, value):
    baggage.set_baggage(key, value)


def get_baggage_item(key):
    return baggage.get_baggage(key)


def get_current_session_id():
    traceparent = get_current_traceparent()
    if traceparent:
        return kv_store.get(f"execution.{traceparent}")
    return None


def set_session_id_from_headers(headers):
    traceparent = headers.get("traceparent")
    session_id = headers.get("session_id")
    if traceparent and session_id:
        set_session_id(session_id, traceparent=traceparent)
        kv_store.set(f"execution.{traceparent}", session_id)
