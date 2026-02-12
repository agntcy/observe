# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

"""SLIM v1.x Instrumentation for OpenTelemetry tracing."""

from typing import Collection
import functools
import json
import base64
import threading

from opentelemetry import baggage, context
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.context import get_value
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from ioa_observe.sdk import TracerWrapper
from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.tracing import set_session_id, get_current_traceparent
from ioa_observe.sdk.tracing.context_utils import _get_agent_linking_info

_instruments = ("slim-bindings >= 1.0.0",)
_global_tracer = None
_kv_lock = threading.RLock()


def _get_session_id(session):
    """Get session ID from session object (v1.x uses .session_id() method)."""
    if hasattr(session, "session_id") and callable(session.session_id):
        try:
            return str(session.session_id())
        except Exception:
            pass
    return str(session.id) if hasattr(session, "id") else None


def _process_received_message(raw_message):
    """Process received message, extract tracing context, return cleaned payload."""
    if raw_message is None:
        return raw_message

    try:
        if isinstance(raw_message, bytes):
            message_dict = json.loads(raw_message.decode())
        elif isinstance(raw_message, str):
            message_dict = json.loads(raw_message)
        elif isinstance(raw_message, dict):
            message_dict = raw_message
        else:
            return raw_message

        headers = message_dict.get("headers", {})
        traceparent = headers.get("traceparent")
        session_id = headers.get("session_id")

        # Restore trace context
        if traceparent:
            carrier = {
                k.lower(): v
                for k, v in headers.items()
                if k.lower() in ["traceparent", "baggage"]
            }
            if carrier:
                ctx = TraceContextTextMapPropagator().extract(carrier=carrier)
                ctx = W3CBaggagePropagator().extract(carrier=carrier, context=ctx)
                context.attach(ctx)

            if session_id and session_id != "None":
                set_session_id(session_id, traceparent=traceparent)
                with _kv_lock:
                    kv_store.set(f"execution.{traceparent}", session_id)

                    # Restore agent linking info for cross-process span linking (agent handoff event)
                    last_agent_span_id = headers.get("last_agent_span_id")
                    last_agent_trace_id = headers.get("last_agent_trace_id")
                    last_agent_name = headers.get("last_agent_name")
                    agent_sequence = headers.get("agent_sequence")

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
                    fork_id = headers.get("fork_id")
                    fork_parent_seq = headers.get("fork_parent_seq")
                    fork_branch_index = headers.get("fork_branch_index")
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

        # Clean headers
        cleaned = message_dict.copy()
        if "headers" in cleaned:
            h = cleaned["headers"].copy()
            for k in [
                "traceparent",
                "session_id",
                "slim_session_id",
                "last_agent_span_id",
                "last_agent_trace_id",
                "last_agent_name",
                "agent_sequence",
                "fork_id",
                "fork_parent_seq",
                "fork_branch_index",
            ]:
                h.pop(k, None)
            if h:
                cleaned["headers"] = h
            else:
                cleaned.pop("headers", None)

        # Extract payload
        if len(cleaned) == 1 and "payload" in cleaned:
            payload = cleaned["payload"]
            if isinstance(payload, str):
                try:
                    return json.dumps(json.loads(payload)).encode("utf-8")
                except json.JSONDecodeError:
                    return payload.encode("utf-8")
            elif isinstance(payload, (dict, list)):
                return json.dumps(payload).encode("utf-8")
            return payload
        return json.dumps(cleaned).encode("utf-8")

    except Exception:
        return raw_message


def _wrap_message_with_headers(message, headers):
    """Wrap message with tracing headers."""
    if isinstance(message, bytes):
        try:
            decoded = message.decode("utf-8")
            try:
                original = json.loads(decoded)
                if isinstance(original, dict):
                    wrapped = original.copy()
                    wrapped["headers"] = {**wrapped.get("headers", {}), **headers}
                else:
                    wrapped = {"headers": headers, "payload": original}
            except json.JSONDecodeError:
                wrapped = {"headers": headers, "payload": decoded}
        except UnicodeDecodeError:
            wrapped = {
                "headers": headers,
                "payload": base64.b64encode(message).decode("utf-8"),
            }
    elif isinstance(message, str):
        try:
            original = json.loads(message)
            if isinstance(original, dict):
                wrapped = original.copy()
                wrapped["headers"] = {**wrapped.get("headers", {}), **headers}
            else:
                wrapped = {"headers": headers, "payload": original}
        except json.JSONDecodeError:
            wrapped = {"headers": headers, "payload": message}
    elif isinstance(message, dict):
        wrapped = message.copy()
        wrapped["headers"] = {**wrapped.get("headers", {}), **headers}
    else:
        wrapped = {"headers": headers, "payload": json.dumps(message)}

    return wrapped


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

        self._instrument_service(slim_bindings)
        self._instrument_app(slim_bindings)
        self._instrument_sessions(slim_bindings)

    def _instrument_service(self, slim_bindings):
        """Instrument SLIM v1.x Service class."""
        if not hasattr(slim_bindings, "Service"):
            return

        Service = slim_bindings.Service

        # connect_async
        if hasattr(Service, "connect_async"):
            original_connect_async = Service.connect_async

            @functools.wraps(original_connect_async)
            async def wrapped_connect_async(self, config, *args, **kwargs):
                if _global_tracer:
                    with _global_tracer.start_as_current_span(
                        "slim.service.connect"
                    ) as span:
                        result = await original_connect_async(
                            self, config, *args, **kwargs
                        )
                        span.set_attribute(
                            "slim.connection.id", str(result) if result else "unknown"
                        )
                        return result
                return await original_connect_async(self, config, *args, **kwargs)

            Service.connect_async = wrapped_connect_async

        # run_server_async
        if hasattr(Service, "run_server_async"):
            original_run_server_async = Service.run_server_async

            @functools.wraps(original_run_server_async)
            async def wrapped_run_server_async(self, config, *args, **kwargs):
                if _global_tracer:
                    with _global_tracer.start_as_current_span(
                        "slim.service.run_server"
                    ) as span:
                        if hasattr(config, "endpoint"):
                            span.set_attribute("slim.server.endpoint", config.endpoint)
                        result = await original_run_server_async(
                            self, config, *args, **kwargs
                        )
                        return result
                return await original_run_server_async(self, config, *args, **kwargs)

            Service.run_server_async = wrapped_run_server_async

    def _instrument_app(self, slim_bindings):
        """Instrument SLIM v1.x App class."""
        if not hasattr(slim_bindings, "App"):
            return

        App = slim_bindings.App

        # create_session_async
        if hasattr(App, "create_session_async"):
            original_create_session_async = App.create_session_async

            @functools.wraps(original_create_session_async)
            async def wrapped_create_session_async(
                self, config, dest=None, *args, **kwargs
            ):
                if _global_tracer:
                    with _global_tracer.start_as_current_span(
                        "slim.app.create_session"
                    ) as span:
                        ctx = await original_create_session_async(
                            self, config, dest, *args, **kwargs
                        )
                        if hasattr(ctx, "session"):
                            sid = _get_session_id(ctx.session)
                            if sid:
                                span.set_attribute("slim.session.id", sid)
                        return ctx
                return await original_create_session_async(
                    self, config, dest, *args, **kwargs
                )

            App.create_session_async = wrapped_create_session_async

        # create_session_and_wait_async
        if hasattr(App, "create_session_and_wait_async"):
            original_create_session_and_wait_async = App.create_session_and_wait_async

            @functools.wraps(original_create_session_and_wait_async)
            async def wrapped_create_session_and_wait_async(
                self, config, dest=None, *args, **kwargs
            ):
                if _global_tracer:
                    with _global_tracer.start_as_current_span(
                        "slim.app.create_session"
                    ) as span:
                        session = await original_create_session_and_wait_async(
                            self, config, dest, *args, **kwargs
                        )
                        sid = _get_session_id(session)
                        if sid:
                            span.set_attribute("slim.session.id", sid)
                        return session
                return await original_create_session_and_wait_async(
                    self, config, dest, *args, **kwargs
                )

            App.create_session_and_wait_async = wrapped_create_session_and_wait_async

        # subscribe_async
        if hasattr(App, "subscribe_async"):
            original_subscribe_async = App.subscribe_async

            @functools.wraps(original_subscribe_async)
            async def wrapped_subscribe_async(self, name, conn_id, *args, **kwargs):
                if _global_tracer:
                    with _global_tracer.start_as_current_span(
                        "slim.app.subscribe"
                    ) as span:
                        if hasattr(name, "organization"):
                            span.set_attribute(
                                "slim.name",
                                f"{name.organization}/{name.namespace}/{name.app}",
                            )
                        return await original_subscribe_async(
                            self, name, conn_id, *args, **kwargs
                        )
                return await original_subscribe_async(
                    self, name, conn_id, *args, **kwargs
                )

            App.subscribe_async = wrapped_subscribe_async

        # set_route_async
        if hasattr(App, "set_route_async"):
            original_set_route_async = App.set_route_async

            @functools.wraps(original_set_route_async)
            async def wrapped_set_route_async(self, name, conn_id, *args, **kwargs):
                if _global_tracer:
                    with _global_tracer.start_as_current_span(
                        "slim.app.set_route"
                    ) as span:
                        if hasattr(name, "organization"):
                            span.set_attribute(
                                "slim.route",
                                f"{name.organization}/{name.namespace}/{name.app}",
                            )
                        return await original_set_route_async(
                            self, name, conn_id, *args, **kwargs
                        )
                return await original_set_route_async(
                    self, name, conn_id, *args, **kwargs
                )

            App.set_route_async = wrapped_set_route_async

        # listen_for_session - this is a blocking call, not async
        if hasattr(App, "listen_for_session"):
            original_listen_for_session = App.listen_for_session

            @functools.wraps(original_listen_for_session)
            def wrapped_listen_for_session(self, *args, **kwargs):
                if _global_tracer:
                    with _global_tracer.start_as_current_span(
                        "slim.app.listen_for_session"
                    ):
                        return original_listen_for_session(self, *args, **kwargs)
                return original_listen_for_session(self, *args, **kwargs)

            App.listen_for_session = wrapped_listen_for_session

    def _instrument_sessions(self, slim_bindings):
        """Instrument session classes for v1.x."""
        session_classes = set()

        # Find session classes
        for name in ["Session", "P2PSession", "GroupSession"]:
            if hasattr(slim_bindings, name):
                session_classes.add(getattr(slim_bindings, name))

        # Find any class with session-like methods
        for name in dir(slim_bindings):
            cls = getattr(slim_bindings, name)
            if isinstance(cls, type) and any(
                hasattr(cls, m) for m in ["get_message_async", "publish_async"]
            ):
                session_classes.add(cls)

        for session_class in session_classes:
            if hasattr(session_class, "get_message_async"):
                self._wrap_get_message(session_class)

            if hasattr(session_class, "publish_async"):
                self._wrap_publish(session_class, "publish_async")

            if hasattr(session_class, "publish_to_async"):
                self._wrap_publish(session_class, "publish_to_async", msg_idx=1)

    def _wrap_get_message(self, session_class):
        """Wrap get_message_async to extract tracing context."""
        orig = session_class.get_message_async

        @functools.wraps(orig)
        async def wrapped(self, *args, **kwargs):
            result = await orig(self, *args, **kwargs)
            if result is None:
                return result

            # v1.x returns ReceivedMessage with .context and .payload
            if hasattr(result, "payload"):
                raw = result.payload
            elif isinstance(result, tuple) and len(result) == 2:
                raw = result[1]
            else:
                raw = result

            processed = _process_received_message(raw)

            if hasattr(result, "payload"):
                try:
                    result.payload = processed
                except AttributeError:

                    class Processed:
                        def __init__(self, ctx, payload):
                            self.context = ctx
                            self.payload = payload

                    return Processed(result.context, processed)
                return result
            elif isinstance(result, tuple) and len(result) == 2:
                return (result[0], processed)
            return processed

        session_class.get_message_async = wrapped

    def _wrap_publish(self, session_class, method_name, msg_idx=0):
        """Wrap publish methods to inject tracing headers."""
        orig = getattr(session_class, method_name)

        @functools.wraps(orig)
        async def wrapped(self, *args, **kwargs):
            traceparent = get_current_traceparent()
            session_id = None

            # Try to get session_id from kv_store using traceparent
            if traceparent:
                with _kv_lock:
                    session_id = kv_store.get(f"execution.{traceparent}")
                    if not session_id:
                        session_id = get_value("session.id")
                        if session_id:
                            kv_store.set(f"execution.{traceparent}", session_id)

            # Fallback: always try to get session_id from context if not found
            if not session_id:
                session_id = get_value("session.id")

            slim_session_id = _get_session_id(self)

            # Get agent linking info for cross-process propagation (agent handoff event)
            agent_linking_info = (
                _get_agent_linking_info(session_id) if session_id else {}
            )

            if _global_tracer:
                with _global_tracer.start_as_current_span(
                    f"session.{method_name}"
                ) as span:
                    # Get traceparent from inside the span context
                    current_traceparent = get_current_traceparent()

                    if slim_session_id:
                        span.set_attribute("slim.session.id", slim_session_id)
                    if session_id:
                        span.set_attribute("session.id", session_id)

                    if (
                        args
                        and len(args) > msg_idx
                        and (current_traceparent or session_id)
                    ):
                        headers = {
                            "session_id": session_id,
                            "traceparent": current_traceparent,
                            "slim_session_id": slim_session_id,
                        }

                        # Add agent linking info for cross-process span linking
                        if agent_linking_info.get("last_agent_span_id"):
                            headers["last_agent_span_id"] = agent_linking_info[
                                "last_agent_span_id"
                            ]
                        if agent_linking_info.get("last_agent_trace_id"):
                            headers["last_agent_trace_id"] = agent_linking_info[
                                "last_agent_trace_id"
                            ]
                        if agent_linking_info.get("last_agent_name"):
                            headers["last_agent_name"] = agent_linking_info[
                                "last_agent_name"
                            ]
                        if agent_linking_info.get("agent_sequence"):
                            headers["agent_sequence"] = agent_linking_info[
                                "agent_sequence"
                            ]

                        # Add fork context for cross-process fork detection
                        if agent_linking_info.get("fork_id"):
                            headers["fork_id"] = agent_linking_info["fork_id"]
                        if agent_linking_info.get("fork_parent_seq"):
                            headers["fork_parent_seq"] = agent_linking_info[
                                "fork_parent_seq"
                            ]
                        if agent_linking_info.get("fork_branch_index"):
                            headers["fork_branch_index"] = agent_linking_info[
                                "fork_branch_index"
                            ]

                        if current_traceparent and session_id:
                            baggage.set_baggage(
                                f"execution.{current_traceparent}", session_id
                            )

                        args_list = list(args)
                        wrapped_msg = _wrap_message_with_headers(
                            args_list[msg_idx], headers
                        )
                        args_list[msg_idx] = (
                            json.dumps(wrapped_msg).encode("utf-8")
                            if isinstance(wrapped_msg, dict)
                            else wrapped_msg
                        )
                        args = tuple(args_list)

                    return await orig(self, *args, **kwargs)
            else:
                if args and len(args) > msg_idx and (traceparent or session_id):
                    headers = {
                        "session_id": session_id,
                        "traceparent": traceparent,
                        "slim_session_id": slim_session_id,
                    }

                    # Add agent linking info for cross-process span linking
                    if agent_linking_info.get("last_agent_span_id"):
                        headers["last_agent_span_id"] = agent_linking_info[
                            "last_agent_span_id"
                        ]
                    if agent_linking_info.get("last_agent_trace_id"):
                        headers["last_agent_trace_id"] = agent_linking_info[
                            "last_agent_trace_id"
                        ]
                    if agent_linking_info.get("last_agent_name"):
                        headers["last_agent_name"] = agent_linking_info[
                            "last_agent_name"
                        ]
                    if agent_linking_info.get("agent_sequence"):
                        headers["agent_sequence"] = agent_linking_info["agent_sequence"]

                    # Add fork context for cross-process fork detection
                    if agent_linking_info.get("fork_id"):
                        headers["fork_id"] = agent_linking_info["fork_id"]
                    if agent_linking_info.get("fork_parent_seq"):
                        headers["fork_parent_seq"] = agent_linking_info[
                            "fork_parent_seq"
                        ]
                    if agent_linking_info.get("fork_branch_index"):
                        headers["fork_branch_index"] = agent_linking_info[
                            "fork_branch_index"
                        ]

                    args_list = list(args)
                    wrapped_msg = _wrap_message_with_headers(
                        args_list[msg_idx], headers
                    )
                    args_list[msg_idx] = (
                        json.dumps(wrapped_msg).encode("utf-8")
                        if isinstance(wrapped_msg, dict)
                        else wrapped_msg
                    )
                    args = tuple(args_list)

                return await orig(self, *args, **kwargs)

        setattr(session_class, method_name, wrapped)

    def _uninstrument(self, **kwargs):
        try:
            import slim_bindings
        except ImportError:
            return

        def restore(obj, methods):
            for m in methods:
                if hasattr(obj, m):
                    orig = getattr(obj, m)
                    if hasattr(orig, "__wrapped__"):
                        setattr(obj, m, orig.__wrapped__)

        if hasattr(slim_bindings, "Service"):
            restore(slim_bindings.Service, ["connect_async", "run_server_async"])

        if hasattr(slim_bindings, "App"):
            restore(
                slim_bindings.App,
                [
                    "create_session_async",
                    "create_session_and_wait_async",
                    "subscribe_async",
                    "set_route_async",
                    "listen_for_session",
                ],
            )

        # Restore session methods
        session_methods = ["publish_async", "publish_to_async", "get_message_async"]
        for name in dir(slim_bindings):
            cls = getattr(slim_bindings, name)
            if isinstance(cls, type):
                restore(cls, session_methods)
