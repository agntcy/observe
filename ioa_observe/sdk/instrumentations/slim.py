# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from typing import Collection
import functools
import json
import base64
import threading

from opentelemetry import baggage, context
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from ioa_observe.sdk import TracerWrapper
from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.tracing import set_session_id, get_current_traceparent

_instruments = ("slim-bindings >= 0.4",)
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

        # Instrument `publish` method - handles multiple signatures
        if hasattr(slim_bindings.Slim, "publish"):
            original_publish = slim_bindings.Slim.publish

            @functools.wraps(original_publish)
            async def instrumented_publish(self, *args, **kwargs):
                if _global_tracer:
                    with _global_tracer.start_as_current_span("slim.publish") as span:
                        traceparent = get_current_traceparent()

                        # Handle different publish signatures
                        # Definition 1: publish(session, message, topic_name) - v0.4.0+ group chat
                        # Definition 2: publish(session, message, organization, namespace, topic) - legacy
                        if len(args) >= 3:
                            session_arg = args[0] if args else None
                            if hasattr(session_arg, "id"):
                                span.set_attribute(
                                    "slim.session.id", str(session_arg.id)
                                )

                            # Check if third argument is PyName (new API) or string (legacy API)
                            if len(args) >= 3 and hasattr(args[2], "organization"):
                                # New API: args[2] is PyName
                                topic_name = args[2]
                                span.set_attribute(
                                    "slim.topic.organization", topic_name.organization
                                )
                                span.set_attribute(
                                    "slim.topic.namespace", topic_name.namespace
                                )
                                span.set_attribute("slim.topic.app", topic_name.app)
                else:
                    traceparent = get_current_traceparent()

                # Thread-safe access to kv_store
                session_id = None
                if traceparent:
                    with _kv_lock:
                        session_id = kv_store.get(f"execution.{traceparent}")
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
                    wrapped_message = SLIMInstrumentor._wrap_message_with_headers(
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

            slim_bindings.Slim.publish = instrumented_publish

        # Instrument `publish_to` (new v0.4.0+ method)
        if hasattr(slim_bindings.Slim, "publish_to"):
            original_publish_to = slim_bindings.Slim.publish_to

            @functools.wraps(original_publish_to)
            async def instrumented_publish_to(
                self, session_info, message, *args, **kwargs
            ):
                if _global_tracer:
                    with _global_tracer.start_as_current_span(
                        "slim.publish_to"
                    ) as span:
                        traceparent = get_current_traceparent()

                        # Add session context to span
                        if hasattr(session_info, "id"):
                            span.set_attribute("slim.session.id", str(session_info.id))
                else:
                    traceparent = get_current_traceparent()

                # Thread-safe access to kv_store
                session_id = None
                if traceparent:
                    with _kv_lock:
                        session_id = kv_store.get(f"execution.{traceparent}")
                        if session_id:
                            kv_store.set(f"execution.{traceparent}", session_id)

                headers = {
                    "session_id": session_id if session_id else None,
                    "traceparent": traceparent,
                    "slim_session_id": str(session_info.id)
                    if hasattr(session_info, "id")
                    else None,
                }

                # Set baggage context
                if traceparent and session_id:
                    baggage.set_baggage(f"execution.{traceparent}", session_id)

                wrapped_message = SLIMInstrumentor._wrap_message_with_headers(
                    self, message, headers
                )
                message_to_send = (
                    json.dumps(wrapped_message).encode("utf-8")
                    if isinstance(wrapped_message, dict)
                    else wrapped_message
                )

                return await original_publish_to(
                    self, session_info, message_to_send, *args, **kwargs
                )

            slim_bindings.Slim.publish_to = instrumented_publish_to

        # Instrument `request_reply` (new v0.4.0+ method)
        if hasattr(slim_bindings.Slim, "request_reply"):
            original_request_reply = slim_bindings.Slim.request_reply

            @functools.wraps(original_request_reply)
            async def instrumented_request_reply(
                self, session_info, message, remote_name, timeout=None, *args, **kwargs
            ):
                if _global_tracer:
                    with _global_tracer.start_as_current_span(
                        "slim.request_reply"
                    ) as span:
                        traceparent = get_current_traceparent()

                        # Add context to span
                        if hasattr(session_info, "id"):
                            span.set_attribute("slim.session.id", str(session_info.id))
                        if hasattr(remote_name, "organization"):
                            span.set_attribute(
                                "slim.remote.organization", remote_name.organization
                            )
                            span.set_attribute(
                                "slim.remote.namespace", remote_name.namespace
                            )
                            span.set_attribute("slim.remote.app", remote_name.app)
                else:
                    traceparent = get_current_traceparent()

                # Thread-safe access to kv_store
                session_id = None
                if traceparent:
                    with _kv_lock:
                        session_id = kv_store.get(f"execution.{traceparent}")
                        if session_id:
                            kv_store.set(f"execution.{traceparent}", session_id)

                headers = {
                    "session_id": session_id if session_id else None,
                    "traceparent": traceparent,
                    "slim_session_id": str(session_info.id)
                    if hasattr(session_info, "id")
                    else None,
                }

                # Set baggage context
                if traceparent and session_id:
                    baggage.set_baggage(f"execution.{traceparent}", session_id)

                wrapped_message = SLIMInstrumentor._wrap_message_with_headers(
                    self, message, headers
                )
                message_to_send = (
                    json.dumps(wrapped_message).encode("utf-8")
                    if isinstance(wrapped_message, dict)
                    else wrapped_message
                )

                kwargs_with_timeout = kwargs.copy()
                if timeout is not None:
                    kwargs_with_timeout["timeout"] = timeout

                return await original_request_reply(
                    self,
                    session_info,
                    message_to_send,
                    remote_name,
                    **kwargs_with_timeout,
                )

            slim_bindings.Slim.request_reply = instrumented_request_reply

        # Instrument `invite` (new v0.4.0+ method for group chat)
        if hasattr(slim_bindings.Slim, "invite"):
            original_invite = slim_bindings.Slim.invite

            @functools.wraps(original_invite)
            async def instrumented_invite(
                self, session_info, participant_name, *args, **kwargs
            ):
                if _global_tracer:
                    with _global_tracer.start_as_current_span("slim.invite") as span:
                        # Add context to span
                        if hasattr(session_info, "id"):
                            span.set_attribute("slim.session.id", str(session_info.id))
                        if hasattr(participant_name, "organization"):
                            span.set_attribute(
                                "slim.participant.organization",
                                participant_name.organization,
                            )
                            span.set_attribute(
                                "slim.participant.namespace", participant_name.namespace
                            )
                            span.set_attribute(
                                "slim.participant.app", participant_name.app
                            )

                return await original_invite(
                    self, session_info, participant_name, *args, **kwargs
                )

            slim_bindings.Slim.invite = instrumented_invite

        # Instrument `set_route` (new v0.4.0+ method)
        if hasattr(slim_bindings.Slim, "set_route"):
            original_set_route = slim_bindings.Slim.set_route

            @functools.wraps(original_set_route)
            async def instrumented_set_route(self, remote_name, *args, **kwargs):
                if _global_tracer:
                    with _global_tracer.start_as_current_span("slim.set_route") as span:
                        # Add context to span
                        if hasattr(remote_name, "organization"):
                            span.set_attribute(
                                "slim.route.organization", remote_name.organization
                            )
                            span.set_attribute(
                                "slim.route.namespace", remote_name.namespace
                            )
                            span.set_attribute("slim.route.app", remote_name.app)

                return await original_set_route(self, remote_name, *args, **kwargs)

            slim_bindings.Slim.set_route = instrumented_set_route

        # Instrument `receive`
        original_receive = slim_bindings.Slim.receive

        @functools.wraps(original_receive)
        async def instrumented_receive(
            self, session=None, timeout=None, *args, **kwargs
        ):
            # Handle both old and new API patterns
            if session is not None or timeout is not None:
                # New API pattern with session parameter
                kwargs_with_params = kwargs.copy()
                if session is not None:
                    kwargs_with_params["session"] = session
                if timeout is not None:
                    kwargs_with_params["timeout"] = timeout
                recv_session, raw_message = await original_receive(
                    self, **kwargs_with_params
                )
            else:
                # Legacy API pattern
                recv_session, raw_message = await original_receive(
                    self, *args, **kwargs
                )

            if raw_message is None:
                return recv_session, raw_message

            try:
                message_dict = json.loads(raw_message.decode())
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
                if carrier and traceparent:
                    ctx = TraceContextTextMapPropagator().extract(carrier=carrier)
                    ctx = W3CBaggagePropagator().extract(carrier=carrier, context=ctx)

                    # Activate the restored context
                    token = context.attach(ctx)

                    try:
                        # Set execution ID with the restored context
                        if session_id and session_id != "None":
                            set_session_id(session_id, traceparent=traceparent)

                            # Store in kv_store with thread safety
                            with _kv_lock:
                                kv_store.set(f"execution.{traceparent}", session_id)
                    finally:
                        context.detach(token)

                # Fallback: check stored execution ID if not found in headers
                if traceparent and (not session_id or session_id == "None"):
                    with _kv_lock:
                        stored_session_id = kv_store.get(f"execution.{traceparent}")
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
                    headers_copy.pop("slim_session_id", None)
                    if headers_copy:
                        message_to_return["headers"] = headers_copy
                    else:
                        message_to_return.pop("headers", None)

                # Return processed message
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
                    return recv_session, json.dumps(message_to_return).encode("utf-8")

            except Exception as e:
                print(f"Error processing message: {e}")
                return recv_session, raw_message

        slim_bindings.Slim.receive = instrumented_receive

        # Instrument `connect`
        original_connect = slim_bindings.Slim.connect

        @functools.wraps(original_connect)
        async def instrumented_connect(self, *args, **kwargs):
            if _global_tracer:
                with _global_tracer.start_as_current_span("slim.connect"):
                    return await original_connect(self, *args, **kwargs)
            else:
                return await original_connect(self, *args, **kwargs)

        slim_bindings.Slim.connect = instrumented_connect

        # Instrument `create_session` (new v0.4.0+ method)
        if hasattr(slim_bindings.Slim, "create_session"):
            original_create_session = slim_bindings.Slim.create_session

            @functools.wraps(original_create_session)
            async def instrumented_create_session(self, config, *args, **kwargs):
                if _global_tracer:
                    with _global_tracer.start_as_current_span(
                        "slim.create_session"
                    ) as span:
                        session_info = await original_create_session(
                            self, config, *args, **kwargs
                        )

                        # Add session attributes to span
                        if hasattr(session_info, "id"):
                            span.set_attribute("slim.session.id", str(session_info.id))

                        return session_info
                else:
                    return await original_create_session(self, config, *args, **kwargs)

            slim_bindings.Slim.create_session = instrumented_create_session

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
            import slim_bindings
        except ImportError:
            raise ImportError(
                "No module named 'slim_bindings'. Please install it first."
            )

        # Restore the original methods
        methods_to_restore = [
            "publish",
            "publish_to",
            "request_reply",
            "receive",
            "connect",
            "create_session",
            "invite",
            "set_route",
        ]

        for method_name in methods_to_restore:
            if hasattr(slim_bindings.Slim, method_name):
                original_method = getattr(slim_bindings.Slim, method_name)
                if hasattr(original_method, "__wrapped__"):
                    setattr(
                        slim_bindings.Slim, method_name, original_method.__wrapped__
                    )
