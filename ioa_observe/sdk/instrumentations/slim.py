from typing import Collection
import functools
import json
import base64
import threading

from opentelemetry import baggage
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from ioa_observe.sdk import TracerWrapper
from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.tracing import set_execution_id, get_current_traceparent

_instruments = ("slim-bindings >= 0.2",)
_global_tracer = None
_kv_lock = threading.RLock()  # Add thread-safety for kv_store operations


class SLIMInstrumentor(BaseInstrumentor):
    def __init__(self):
        super().__init__()
        global _global_tracer
        _global_tracer = TracerWrapper().get_tracer()

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        try:
            import slim_bindings
        except ImportError:
            raise ImportError(
                "No module named 'slim_bindings'. Please install it first."
            )

        # Instrument `publish`
        original_publish = slim_bindings.Slim.publish

        @functools.wraps(original_publish)
        async def instrumented_publish(
            self, session, message, organization, namespace, topic, *args, **kwargs
        ):
            with _global_tracer.start_as_current_span("slim.publish"):
                # Use the helper function for consistent traceparent
                traceparent = get_current_traceparent()

                if traceparent:
                    # Retrieve execution_id using the same traceparent
                    print("traceparent in publish method:", traceparent)

            # Thread-safe access to kv_store
            execution_id = None
            if traceparent:
                with _kv_lock:
                    execution_id = kv_store.get(f"execution.{traceparent}")
                    if execution_id:
                        kv_store.set(f"execution.{traceparent}", execution_id)

            print(
                f"Publishing message with traceparent: {traceparent}, execution_id: {execution_id}"
            )
            # Add tracing context to the message headers
            headers = {
                "execution_id": execution_id if execution_id else None,
                "traceparent": traceparent,
            }

            # Set baggage context
            if traceparent and execution_id:
                baggage.set_baggage(f"execution.{traceparent}", execution_id)

            # Process message payload
            if isinstance(message, bytes):
                try:
                    decoded_message = message.decode("utf-8")
                    try:
                        json.loads(decoded_message)
                        payload = decoded_message
                    except json.JSONDecodeError:
                        payload = decoded_message
                except UnicodeDecodeError:
                    payload = base64.b64encode(message).decode("utf-8")
            elif isinstance(message, str):
                payload = message
            else:
                payload = json.dumps(message)

            wrapped_message = {
                "headers": headers,
                "payload": payload,
            }

            message_to_send = json.dumps(wrapped_message).encode("utf-8")

            return await original_publish(
                self,
                session,
                message_to_send,
                organization,
                namespace,
                topic,
                *args,
                **kwargs,
            )

        slim_bindings.Slim.publish = instrumented_publish

        # Instrument `receive`
        original_receive = slim_bindings.Slim.receive

        @functools.wraps(original_receive)
        async def instrumented_receive(self, session, *args, **kwargs):
            recv_session, raw_message = await original_receive(
                self, session, *args, **kwargs
            )

            if raw_message is None:
                return recv_session, raw_message

            try:
                message_dict = json.loads(raw_message.decode())
                headers = message_dict.get("headers", {})

                # Extract traceparent from headers
                traceparent = headers.get("traceparent")
                execution_id = headers.get("execution_id")

                print(
                    f"Received message with traceparent: {traceparent}, execution_id: {execution_id}"
                )

                # First, extract and restore the trace context from headers
                carrier = {}
                for key in ["traceparent", "Traceparent", "baggage", "Baggage"]:
                    if key.lower() in [k.lower() for k in headers.keys()]:
                        for k in headers.keys():
                            if k.lower() == key.lower():
                                carrier[key.lower()] = headers[k]

                # Restore the trace context BEFORE calling set_execution_id
                if carrier and traceparent:
                    ctx = TraceContextTextMapPropagator().extract(carrier=carrier)
                    ctx = W3CBaggagePropagator().extract(carrier=carrier, context=ctx)

                    # Now set execution ID with the restored context
                    if execution_id and execution_id != "None":
                        print(
                            f"Setting execution ID with restored context: {execution_id} for traceparent: {traceparent}"
                        )
                        # Pass the traceparent explicitly to prevent new context creation
                        set_execution_id(execution_id, traceparent=traceparent)

                        # Store in kv_store with thread safety
                        with _kv_lock:
                            kv_store.set(f"execution.{traceparent}", execution_id)

                # Fallback: check stored execution ID if not found in headers
                if traceparent and (not execution_id or execution_id == "None"):
                    with _kv_lock:
                        stored_execution_id = kv_store.get(f"execution.{traceparent}")
                        print(
                            f"Stored execution ID for traceparent {traceparent}: {stored_execution_id}"
                        )
                        if stored_execution_id:
                            execution_id = stored_execution_id
                            set_execution_id(execution_id, traceparent=traceparent)

                # Process payload
                payload = message_dict.get("payload", raw_message)
                if isinstance(payload, str):
                    try:
                        payload_dict = json.loads(payload)
                        return recv_session, json.dumps(payload_dict).encode("utf-8")
                    except json.JSONDecodeError:
                        return recv_session, payload.encode("utf-8") if isinstance(
                            payload, str
                        ) else payload

                return recv_session, json.dumps(payload).encode("utf-8") if isinstance(
                    payload, (dict, list)
                ) else payload

            except Exception as e:
                print(f"Error processing message: {e}")
                return recv_session, raw_message

        slim_bindings.Slim.receive = instrumented_receive

        # Instrument `connect`
        original_connect = slim_bindings.Slim.connect

        @functools.wraps(original_connect)
        async def instrumented_connect(self, *args, **kwargs):
            return await original_connect(self, *args, **kwargs)

        slim_bindings.Slim.connect = instrumented_connect

    def _uninstrument(self, **kwargs):
        try:
            import slim_bindings
        except ImportError:
            raise ImportError(
                "No module named 'slim_bindings'. Please install it first."
            )

        # Restore the original methods
        slim_bindings.Slim.publish = slim_bindings.Slim.publish.__wrapped__
        slim_bindings.Slim.receive = slim_bindings.Slim.receive.__wrapped__
        slim_bindings.Slim.connect = slim_bindings.Slim.connect.__wrapped__
