# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

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
from ioa_observe.sdk.tracing import set_session_id, get_current_traceparent

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

            # Thread-safe access to kv_store
            session_id = None
            if traceparent:
                with _kv_lock:
                    session_id = kv_store.get(f"execution.{traceparent}")
                    if session_id:
                        kv_store.set(f"execution.{traceparent}", session_id)
            # Add tracing context to the message headers
            headers = {
                "session_id": session_id if session_id else None,
                "traceparent": traceparent,
            }

            # Set baggage context
            if traceparent and session_id:
                baggage.set_baggage(f"execution.{traceparent}", session_id)

            # Process message payload and preserve original structure
            if isinstance(message, bytes):
                try:
                    decoded_message = message.decode("utf-8")
                    try:
                        # If it's already a JSON structure, preserve it
                        original_message = json.loads(decoded_message)
                        if isinstance(original_message, dict):
                            # Preserve all original fields and merge/update headers
                            wrapped_message = original_message.copy()
                            existing_headers = wrapped_message.get("headers", {})
                            existing_headers.update(headers)
                            wrapped_message["headers"] = existing_headers
                        else:
                            # If it's not a dict, wrap it as payload
                            wrapped_message = {
                                "headers": headers,
                                "payload": original_message,
                            }
                    except json.JSONDecodeError:
                        # If it's not JSON, treat as raw payload
                        wrapped_message = {
                            "headers": headers,
                            "payload": decoded_message,
                        }
                except UnicodeDecodeError:
                    # If it can't be decoded, base64 encode it
                    wrapped_message = {
                        "headers": headers,
                        "payload": base64.b64encode(message).decode("utf-8"),
                    }
            elif isinstance(message, str):
                try:
                    # Try to parse as JSON first
                    original_message = json.loads(message)
                    if isinstance(original_message, dict):
                        # Preserve all original fields and merge/update headers
                        wrapped_message = original_message.copy()
                        existing_headers = wrapped_message.get("headers", {})
                        existing_headers.update(headers)
                        wrapped_message["headers"] = existing_headers
                    else:
                        # If it's not a dict, wrap it as payload
                        wrapped_message = {
                            "headers": headers,
                            "payload": original_message,
                        }
                except json.JSONDecodeError:
                    # If it's not JSON, treat as raw payload
                    wrapped_message = {
                        "headers": headers,
                        "payload": message,
                    }
            elif isinstance(message, dict):
                # If it's already a dict, preserve all fields and merge headers
                wrapped_message = message.copy()
                existing_headers = wrapped_message.get("headers", {})
                existing_headers.update(headers)
                wrapped_message["headers"] = existing_headers
            else:
                # For other types, convert to JSON and wrap as payload
                wrapped_message = {
                    "headers": headers,
                    "payload": json.dumps(message),
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
                session_id = headers.get("session_id")

                # First, extract and restore the trace context from headers
                carrier = {}
                for key in ["traceparent", "Traceparent", "baggage", "Baggage"]:
                    if key.lower() in [k.lower() for k in headers.keys()]:
                        for k in headers.keys():
                            if k.lower() == key.lower():
                                carrier[key.lower()] = headers[k]

                # Restore the trace context BEFORE calling set_session_id
                if carrier and traceparent:
                    ctx = TraceContextTextMapPropagator().extract(carrier=carrier)
                    ctx = W3CBaggagePropagator().extract(carrier=carrier, context=ctx)

                    # Now set execution ID with the restored context
                    if session_id and session_id != "None":
                        # Pass the traceparent explicitly to prevent new context creation
                        set_session_id(session_id, traceparent=traceparent)

                        # Store in kv_store with thread safety
                        with _kv_lock:
                            kv_store.set(f"execution.{traceparent}", session_id)

                # Fallback: check stored execution ID if not found in headers
                if traceparent and (not session_id or session_id == "None"):
                    with _kv_lock:
                        stored_session_id = kv_store.get(f"execution.{traceparent}")
                        if stored_session_id:
                            session_id = stored_session_id
                            set_session_id(session_id, traceparent=traceparent)

                # Process the complete message structure
                # Remove tracing headers before returning the message
                message_to_return = message_dict.copy()
                if "headers" in message_to_return:
                    headers_copy = message_to_return["headers"].copy()
                    # Remove tracing-specific headers but keep other headers
                    headers_copy.pop("traceparent", None)
                    headers_copy.pop("session_id", None)
                    if headers_copy:
                        message_to_return["headers"] = headers_copy
                    else:
                        message_to_return.pop("headers", None)

                # If the message only contains a payload field and no other fields,
                # return just the payload for backward compatibility
                if len(message_to_return) == 1 and "payload" in message_to_return:
                    payload = message_to_return["payload"]
                    if isinstance(payload, str):
                        try:
                            payload_dict = json.loads(payload)
                            return recv_session, json.dumps(payload_dict).encode(
                                "utf-8"
                            )
                        except json.JSONDecodeError:
                            return recv_session, payload.encode("utf-8") if isinstance(
                                payload, str
                            ) else payload
                    return recv_session, json.dumps(payload).encode(
                        "utf-8"
                    ) if isinstance(payload, (dict, list)) else payload
                else:
                    # Return the complete message structure with all original fields
                    return recv_session, json.dumps(message_to_return).encode("utf-8")

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
