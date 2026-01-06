# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from typing import Collection
import functools
import json
import base64
import threading

from opentelemetry.context import get_value
from opentelemetry import baggage, context
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from ioa_observe.sdk import TracerWrapper
from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.tracing import set_session_id, get_current_traceparent

_instruments = ("nats-py >= 2.10.0",)
_global_tracer = None
_kv_lock = threading.RLock()  # Add thread-safety for kv_store operations


class NATSInstrumentor(BaseInstrumentor):
    def __init__(self):
        super().__init__()
        global _global_tracer
        _global_tracer = TracerWrapper().get_tracer()

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        try:
            import nats
        except ImportError:
            raise ImportError("No module named 'nats'. Please install it first.")

        # Instrument `publish` method
        original_publish = nats.NATS.publish

        @functools.wraps(original_publish)
        async def instrumented_publish(self, *args, **kwargs):
            if _global_tracer:
                with _global_tracer.start_as_current_span("nats.publish") as span:
                    traceparent = get_current_traceparent()
                    span.set_attribute("nats.topic", args[0] if args else None)
            else:
                traceparent = get_current_traceparent()

            # Thread-safe access to kv_store
            session_id = None
            if traceparent:
                with _kv_lock:
                    session_id = kv_store.get(f"execution.{traceparent}")
                    if not session_id:
                        session_id = get_value("session.id")
                        if session_id:
                            kv_store.set(f"execution.{traceparent}", session_id)

            headers = {
                "session_id": session_id if session_id else None,
                "traceparent": traceparent,
            }

            # Set baggage context
            if traceparent and session_id:
                baggage.set_baggage(f"execution.{traceparent}", session_id)

            # Wrap message with headers - handle different message positions
            message_arg_index = 1  # message will typically be the second argument
            if len(args) > message_arg_index:
                original_args = list(args)
                message = original_args[message_arg_index]
                wrapped_message = NATSInstrumentor._wrap_message_with_headers(
                    self, message, headers
                )

                # Convert wrapped message back to bytes if needed
                if isinstance(wrapped_message, dict):
                    message_to_send = json.dumps(wrapped_message).encode("utf-8")
                else:
                    message_to_send = wrapped_message

                original_args[message_arg_index] = message_to_send
                args = tuple(original_args)

            return await original_publish(self, *args, **kwargs)

        nats.NATS.publish = instrumented_publish

        # Instrument `request` method
        original_request = nats.NATS.request

        @functools.wraps(original_request)
        async def instrumented_request(self, *args, **kwargs):
            if _global_tracer:
                with _global_tracer.start_as_current_span("nats.request") as span:
                    traceparent = get_current_traceparent()
                    span.set_attribute("nats.topic", args[0] if args else None)
            else:
                traceparent = get_current_traceparent()

            # Thread-safe access to kv_store
            session_id = None
            if traceparent:
                with _kv_lock:
                    session_id = kv_store.get(f"execution.{traceparent}")
                    if not session_id:
                        session_id = get_value("session.id")
                        if session_id:
                            kv_store.set(f"execution.{traceparent}", session_id)

            headers = {
                "session_id": session_id if session_id else None,
                "traceparent": traceparent,
            }

            # Set baggage context
            if traceparent and session_id:
                baggage.set_baggage(f"execution.{traceparent}", session_id)

            # Wrap message with headers - handle different message positions
            message_arg_index = 1  # message will typically be the second argument
            if len(args) > message_arg_index:
                original_args = list(args)
                message = original_args[message_arg_index]
                wrapped_message = NATSInstrumentor._wrap_message_with_headers(
                    self, message, headers
                )

                # Convert wrapped message back to bytes if needed
                if isinstance(wrapped_message, dict):
                    message_to_send = json.dumps(wrapped_message).encode("utf-8")
                else:
                    message_to_send = wrapped_message

                original_args[message_arg_index] = message_to_send
                args = tuple(original_args)

            return await original_request(self, *args, **kwargs)

        nats.NATS.request = instrumented_request

        # Instrument `subscribe` method
        original_subscribe = nats.NATS.subscribe

        @functools.wraps(original_subscribe)
        async def instrumented_subscribe(self, subject, cb=None, *args, **kwargs):
            # Wrap the callback to add tracing spans for message handling
            if (
                cb is not None
                and _global_tracer
                and not getattr(cb, "_is_instrumented", False)
            ):
                user_cb = cb  # SAVE the original callback

                @functools.wraps(user_cb)
                async def traced_callback(msg):
                    try:
                        message_dict = json.loads(msg.data.decode())
                        headers = message_dict.get("headers", {})

                        # Extract traceparent and session info from headers
                        traceparent = headers.get("traceparent")
                        session_id = headers.get("session_id")

                        # Create carrier for context propagation
                        carrier = {}
                        for key in ["traceparent", "Traceparent", "baggage", "Baggage"]:
                            if key.lower() in [k.lower() for k in headers.keys()]:
                                for k in headers.keys():
                                    if k.lower() == key.lower():
                                        carrier[key.lower()] = headers[k]

                        # Restore trace context
                        ctx = None
                        if carrier and traceparent:
                            ctx = TraceContextTextMapPropagator().extract(
                                carrier=carrier
                            )
                            ctx = W3CBaggagePropagator().extract(
                                carrier=carrier, context=ctx
                            )

                            # Activate the restored context
                            token = context.attach(ctx)

                            try:
                                # Set execution ID with the restored context
                                if session_id and session_id != "None":
                                    set_session_id(session_id, traceparent=traceparent)

                                    # Store in kv_store with thread safety
                                    with _kv_lock:
                                        kv_store.set(
                                            f"execution.{traceparent}", session_id
                                        )

                                # DON'T detach the context yet - we need it to persist for the callback
                                # The context will be cleaned up later or by the garbage collector

                            except Exception as e:
                                # Only detach on error
                                context.detach(token)
                                raise e
                        elif traceparent and session_id and session_id != "None":
                            # Even without carrier context, set session ID if we have the data
                            set_session_id(session_id, traceparent=traceparent)

                        # Fallback: check stored execution ID if not found in headers
                        if traceparent and (not session_id or session_id == "None"):
                            with _kv_lock:
                                stored_session_id = kv_store.get(
                                    f"execution.{traceparent}"
                                )
                                if stored_session_id:
                                    session_id = stored_session_id
                                    set_session_id(session_id, traceparent=traceparent)

                        # Process and clean the message
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

                        # Return processed message, update msg.data
                        if isinstance(message_to_return, str):
                            msg.data = message_to_return.encode("utf-8")
                        else:
                            msg.data = json.dumps(message_to_return).encode("utf-8")

                        # Now call the original user callback with the modified msg
                        ctx = {} if ctx is None else ctx
                        if _global_tracer:
                            with _global_tracer.start_as_current_span(
                                "nats.subscribe.callback", context=ctx
                            ) as span:
                                span.set_attribute("nats.subject", subject)
                                span.set_attribute("nats.session_id", session_id)
                                await user_cb(msg)
                        else:
                            await user_cb(msg)
                    except Exception as e:
                        print(f"Error processing message in traced_callback: {e}")
                        await user_cb(msg)  # Call original callback even on error

                traced_callback._is_instrumented = True  # mark as instrumented
                cb = traced_callback

            return await original_subscribe(self, subject, cb=cb, *args, **kwargs)

        nats.NATS.subscribe = instrumented_subscribe

    def _wrap_message_with_headers(self, message, headers):
        """Helper method to wrap messages with headers consistently"""
        if isinstance(message, bytes):
            try:
                decoded_message = message.decode("utf-8")
                try:
                    original_message = json.loads(decoded_message)
                    if isinstance(original_message, dict):
                        wrapped_message = original_message.copy()
                        existing_headers = wrapped_message.get("headers", {})
                        existing_headers.update(headers)
                        wrapped_message["headers"] = existing_headers
                    else:
                        wrapped_message = {
                            "headers": headers,
                            "payload": original_message,
                        }
                except json.JSONDecodeError:
                    wrapped_message = {"headers": headers, "payload": decoded_message}
            except UnicodeDecodeError:
                # Fix type annotation issue by ensuring message is bytes
                encoded_message = (
                    message if isinstance(message, bytes) else message.encode("utf-8")
                )
                wrapped_message = {
                    "headers": headers,
                    "payload": base64.b64encode(encoded_message).decode("utf-8"),
                }
        elif isinstance(message, str):
            try:
                original_message = json.loads(message)
                if isinstance(original_message, dict):
                    wrapped_message = original_message.copy()
                    existing_headers = wrapped_message.get("headers", {})
                    existing_headers.update(headers)
                    wrapped_message["headers"] = existing_headers
                else:
                    wrapped_message = {"headers": headers, "payload": original_message}
            except json.JSONDecodeError:
                wrapped_message = {"headers": headers, "payload": message}
        elif isinstance(message, dict):
            wrapped_message = message.copy()
            existing_headers = wrapped_message.get("headers", {})
            existing_headers.update(headers)
            wrapped_message["headers"] = existing_headers
        else:
            wrapped_message = {"headers": headers, "payload": json.dumps(message)}

        return wrapped_message

    def _uninstrument(self, **kwargs):
        try:
            import nats
        except ImportError:
            raise ImportError("No module named 'nats'. Please install it first.")

        # Restore the original methods
        methods_to_restore = [
            "publish",
            "request",
            "subscribe",
        ]

        for method_name in methods_to_restore:
            if hasattr(nats.NATS, method_name):
                original_method = getattr(nats.NATS, method_name)
                if hasattr(original_method, "__wrapped__"):
                    setattr(nats.NATS, method_name, original_method.__wrapped__)
