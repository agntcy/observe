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
from ioa_observe.sdk.tracing.context_manager import get_tracer

_instruments = ("slim-bindings >= 0.4.0",)
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
        # In v0.6.0+, publish moved from Slim class to Session objects
        if hasattr(slim_bindings.Slim, "publish"):
            # Legacy v0.5.x app-level publish method
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
                        if not session_id:
                            session_id = get_value("session.id")
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

        # Instrument `request_reply` (v0.4.0+ to v0.5.x method, removed in v0.6.0)
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
                        if not session_id:
                            session_id = get_value("session.id")
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

        # Instrument `receive` - only if it exists (removed in v0.6.0)
        if hasattr(slim_bindings.Slim, "receive"):
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
                                    kv_store.set(f"execution.{traceparent}", session_id)

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
                                return recv_session, payload.encode(
                                    "utf-8"
                                ) if isinstance(payload, str) else payload
                        return recv_session, json.dumps(payload).encode(
                            "utf-8"
                        ) if isinstance(payload, (dict, list)) else payload
                    else:
                        return recv_session, json.dumps(message_to_return).encode(
                            "utf-8"
                        )

                except Exception as e:
                    print(f"Error processing message: {e}")
                    return recv_session, raw_message

            slim_bindings.Slim.receive = instrumented_receive

        # Instrument `connect` - only if it exists
        if hasattr(slim_bindings.Slim, "connect"):
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

        # Instrument new v0.6.0+ session-level methods
        # These methods are available on Session objects, not the Slim app
        self._instrument_session_methods(slim_bindings)

        # Instrument new v0.6.0+ app-level methods
        # listen_for_session replaces app.receive() for new sessions in v0.6.0+
        if hasattr(slim_bindings.Slim, "listen_for_session"):
            original_listen_for_session = slim_bindings.Slim.listen_for_session

            @functools.wraps(original_listen_for_session)
            async def instrumented_listen_for_session(self, *args, **kwargs):
                if _global_tracer:
                    with _global_tracer.start_as_current_span(
                        "slim.listen_for_session"
                    ):
                        session = await original_listen_for_session(
                            self, *args, **kwargs
                        )

                        return session
                else:
                    return await original_listen_for_session(self, *args, **kwargs)

            slim_bindings.Slim.listen_for_session = instrumented_listen_for_session

    def _instrument_session_methods(self, slim_bindings):
        # Try to find session-related classes in the slim_bindings module
        session_classes = []

        # Check for v0.6.0+ Session classes
        if hasattr(slim_bindings, "Session"):
            for attr_name in ["Session", "P2PSession", "GroupSession"]:
                if hasattr(slim_bindings, attr_name):
                    session_class = getattr(slim_bindings, attr_name)
                    session_classes.append((attr_name, session_class))

        # Check for older PySession class (pre-v0.6.0)
        if hasattr(slim_bindings, "PySession"):
            session_classes.append(("PySession", slim_bindings.PySession))

        # Also look for any class that has session-like methods
        for attr_name in dir(slim_bindings):
            attr = getattr(slim_bindings, attr_name)
            if isinstance(attr, type) and (
                hasattr(attr, "get_message") or hasattr(attr, "publish")
            ):
                session_classes.append((attr_name, attr))

        # Instrument session methods for found classes
        for class_name, session_class in session_classes:
            # Instrument get_message (v0.6.0+ replacement for receive)
            if hasattr(session_class, "get_message"):
                self._instrument_session_get_message(session_class)

            # Instrument session publish methods
            if hasattr(session_class, "publish"):
                self._instrument_session_publish(session_class, "publish")

            if hasattr(session_class, "publish_to"):
                self._instrument_session_publish(session_class, "publish_to")

    def _instrument_session_get_message(self, session_class):
        """Instrument session.get_message method (v0.6.0+)"""
        original_get_message = session_class.get_message

        @functools.wraps(original_get_message)
        async def instrumented_get_message(self, timeout=None, *args, **kwargs):
            # Handle the message reception similar to the old receive method
            if timeout is not None:
                kwargs["timeout"] = timeout

            result = await original_get_message(self, **kwargs)

            # Handle different return types from get_message
            if result is None:
                return result

            # Check if get_message returns a tuple (context, message) or just message
            if isinstance(result, tuple) and len(result) == 2:
                message_context, raw_message = result
            else:
                raw_message = result
                message_context = None

            if raw_message is None:
                return result

            try:
                # Handle different message types
                if isinstance(raw_message, bytes):
                    message_dict = json.loads(raw_message.decode())
                elif isinstance(raw_message, str):
                    message_dict = json.loads(raw_message)
                elif isinstance(raw_message, dict):
                    message_dict = raw_message
                else:
                    # Unknown type, return as-is
                    return result

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

                        # DON'T detach the context yet - we need it to persist for the callback

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

                # Return processed message, maintaining original return format
                if len(message_to_return) == 1 and "payload" in message_to_return:
                    payload = message_to_return["payload"]
                    if isinstance(payload, str):
                        try:
                            payload_dict = json.loads(payload)
                            processed_message = json.dumps(payload_dict).encode("utf-8")
                        except json.JSONDecodeError:
                            processed_message = (
                                payload.encode("utf-8")
                                if isinstance(payload, str)
                                else payload
                            )
                    else:
                        processed_message = (
                            json.dumps(payload).encode("utf-8")
                            if isinstance(payload, (dict, list))
                            else payload
                        )
                else:
                    processed_message = json.dumps(message_to_return).encode("utf-8")

                # Return in the same format as received
                if isinstance(result, tuple) and len(result) == 2:
                    return (message_context, processed_message)
                else:
                    return processed_message

            except Exception as e:
                print(f"Error processing message: {e}")
                return result

        session_class.get_message = instrumented_get_message

    def _instrument_session_publish(self, session_class, method_name):
        """Instrument session publish methods"""
        original_method = getattr(session_class, method_name)

        @functools.wraps(original_method)
        async def instrumented_session_publish(self, *args, **kwargs):
            if _global_tracer:
                with _global_tracer.start_as_current_span(
                    f"session.{method_name}"
                ) as span:
                    traceparent = get_current_traceparent()

                    # Add session context to span
                    if hasattr(self, "id"):
                        span.set_attribute("slim.session.id", str(self.id))

                    # Handle message wrapping
                    if args:
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
                            "slim_session_id": str(self.id)
                            if hasattr(self, "id")
                            else None,
                        }

                        # Set baggage context
                        if traceparent and session_id:
                            baggage.set_baggage(f"execution.{traceparent}", session_id)

                        # Wrap the message (first argument for publish, second for publish_to)
                        # If the session_class is SessionContext, the message is always in second position
                        message_idx = (
                            1
                            if method_name == "publish_to"
                            or session_class.__name__ == "SessionContext"
                            else 0
                        )
                        if len(args) > message_idx:
                            args_list = list(args)
                            message = args_list[message_idx]
                            wrapped_message = (
                                SLIMInstrumentor._wrap_message_with_headers(
                                    None, message, headers
                                )
                            )

                            # Convert wrapped message back to bytes if needed
                            if isinstance(wrapped_message, dict):
                                message_to_send = json.dumps(wrapped_message).encode(
                                    "utf-8"
                                )
                            else:
                                message_to_send = wrapped_message

                            args_list[message_idx] = message_to_send
                            args = tuple(args_list)

                    return await original_method(self, *args, **kwargs)
            else:
                # Handle message wrapping even without tracing
                if args:
                    traceparent = get_current_traceparent()
                    session_id = None
                    if traceparent:
                        with _kv_lock:
                            session_id = kv_store.get(f"execution.{traceparent}")
                            if not session_id:
                                session_id = get_value("session.id")
                                if session_id:
                                    kv_store.set(f"execution.{traceparent}", session_id)

                    if traceparent or session_id:
                        headers = {
                            "session_id": session_id if session_id else None,
                            "traceparent": traceparent,
                            "slim_session_id": str(self.id)
                            if hasattr(self, "id")
                            else None,
                        }

                        # Wrap the message (first argument for publish, second for publish_to)
                        message_idx = 1 if method_name == "publish_to" else 0
                        if len(args) > message_idx:
                            args_list = list(args)
                            message = args_list[message_idx]
                            wrapped_message = (
                                SLIMInstrumentor._wrap_message_with_headers(
                                    None, message, headers
                                )
                            )

                            if isinstance(wrapped_message, dict):
                                message_to_send = json.dumps(wrapped_message).encode(
                                    "utf-8"
                                )
                            else:
                                message_to_send = wrapped_message

                            args_list[message_idx] = message_to_send
                            args = tuple(args_list)

                return await original_method(self, *args, **kwargs)

        setattr(session_class, method_name, instrumented_session_publish)

    def _instrument_session_method_if_exists(self, slim_bindings, method_name):
        """Helper to instrument a session method if it exists"""

        # Look for session classes that might have this method
        for attr_name in dir(slim_bindings):
            attr = getattr(slim_bindings, attr_name)
            if hasattr(attr, method_name):
                original_method = getattr(attr, method_name)

                if callable(original_method):
                    instrumented_method = self._create_session_method_wrapper(
                        method_name, original_method
                    )
                    setattr(attr, method_name, instrumented_method)

    def _create_session_method_wrapper(self, method_name, original_method):
        """Create an instrumented wrapper for session methods"""

        @functools.wraps(original_method)
        async def instrumented_session_method(self, *args, **kwargs):
            if _global_tracer:
                tracer = get_tracer()
                with tracer.start_as_current_span(f"session.{method_name}"):
                    traceparent = get_current_traceparent()

                    # Handle message wrapping for publish methods
                    if method_name in ["publish", "publish_to"] and args:
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
                            "slim_session_id": str(self.id)
                            if hasattr(self, "id")
                            else None,
                        }

                        # Set baggage context
                        if traceparent and session_id:
                            baggage.set_baggage(f"execution.{traceparent}", session_id)

                        # Wrap the message (first argument for publish, second for publish_to)
                        message_idx = 1 if method_name == "publish_to" else 0
                        if len(args) > message_idx:
                            args_list = list(args)
                            message = args_list[message_idx]
                            wrapped_message = SLIMInstrumentor._wrap_message_with_headers(
                                None,
                                message,
                                headers,  # Pass None for self since it's a static method
                            )

                            # Convert wrapped message back to bytes if needed
                            if isinstance(wrapped_message, dict):
                                message_to_send = json.dumps(wrapped_message).encode(
                                    "utf-8"
                                )
                            else:
                                message_to_send = wrapped_message

                            args_list[message_idx] = message_to_send
                            args = tuple(args_list)

                    return await original_method(self, *args, **kwargs)
            else:
                # Handle message wrapping even without tracing
                if method_name in ["publish", "publish_to"] and args:
                    traceparent = get_current_traceparent()
                    session_id = None
                    if traceparent:
                        with _kv_lock:
                            session_id = kv_store.get(f"execution.{traceparent}")
                            if not session_id:
                                session_id = get_value("session.id")
                                if session_id:
                                    kv_store.set(f"execution.{traceparent}", session_id)

                    if traceparent or session_id:
                        headers = {
                            "session_id": session_id if session_id else None,
                            "traceparent": traceparent,
                            "slim_session_id": str(self.id)
                            if hasattr(self, "id")
                            else None,
                        }

                        # Wrap the message
                        message_idx = 1 if method_name == "publish_to" else 0
                        if len(args) > message_idx:
                            args_list = list(args)
                            message = args_list[message_idx]
                            wrapped_message = (
                                SLIMInstrumentor._wrap_message_with_headers(
                                    None, message, headers
                                )
                            )

                            if isinstance(wrapped_message, dict):
                                message_to_send = json.dumps(wrapped_message).encode(
                                    "utf-8"
                                )
                            else:
                                message_to_send = wrapped_message

                            args_list[message_idx] = message_to_send
                            args = tuple(args_list)

                return await original_method(self, *args, **kwargs)

        return instrumented_session_method

    @staticmethod
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
            "request_reply",  # v0.4.0-v0.5.x only
            "receive",
            "connect",
            "create_session",
            "invite",
            "set_route",
            "listen_for_session",  # v0.6.0+
        ]

        for method_name in methods_to_restore:
            if hasattr(slim_bindings.Slim, method_name):
                original_method = getattr(slim_bindings.Slim, method_name)
                if hasattr(original_method, "__wrapped__"):
                    setattr(
                        slim_bindings.Slim, method_name, original_method.__wrapped__
                    )

        # Also try to restore session-level methods (v0.6.0+)
        # This is best-effort since session classes may vary
        session_methods_to_restore = [
            "publish",
            "publish_to",
            "get_message",
            "invite",
            "remove",
        ]

        for attr_name in dir(slim_bindings):
            attr = getattr(slim_bindings, attr_name)
            for method_name in session_methods_to_restore:
                if hasattr(attr, method_name):
                    original_method = getattr(attr, method_name)
                    if hasattr(original_method, "__wrapped__"):
                        setattr(attr, method_name, original_method.__wrapped__)
