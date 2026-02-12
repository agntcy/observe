# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import json
import time
import traceback
from functools import wraps
import os
import types
from typing import Optional, TypeVar, Callable, Any
import inspect

from ioa_observe.sdk.decorators.helpers import (
    _is_async_method,
    _get_original_function_name,
    _is_async_generator,
)

from langgraph.graph.state import CompiledStateGraph
from opentelemetry import trace
from opentelemetry import context as context_api
from opentelemetry.context import get_value, attach, set_value
from pydantic_core import PydanticSerializationError
from typing_extensions import ParamSpec

from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.decorators.fork_join import (
    detect_fork_on_agent_start,
    detect_join_and_build_links,
    detect_tool_fork_on_start,
    find_parent_agent_seq,
    get_agent_record,
    make_single_link,
    mark_agent_ended,
    mark_tool_ended_for_fork,
    register_active_span,
    unregister_active_span,
    annotate_fork_branch,
    annotate_join,
    get_fork_parent_seq,
)
from ioa_observe.sdk.decorators.util import determine_workflow_type, _serialize_object
from ioa_observe.sdk.metrics.agents.availability import agent_availability
from ioa_observe.sdk.metrics.agents.recovery_tracker import agent_recovery_tracker
from ioa_observe.sdk.metrics.agents.tool_call_tracker import tool_call_tracker
from ioa_observe.sdk.metrics.agents.tracker import connection_tracker
from ioa_observe.sdk.metrics.agents.heuristics import compute_agent_interpretation_score
from ioa_observe.sdk.telemetry import Telemetry
from ioa_observe.sdk.tracing import get_tracer, set_workflow_name
from ioa_observe.sdk.tracing.tracing import (
    TracerWrapper,
    set_entity_path,
    get_chained_entity_path,
    set_agent_id_event,
    set_application_id,
)
from ioa_observe.sdk.metrics.agents.agent_connections import connection_reliability
from ioa_observe.sdk.utils import camel_to_snake
from ioa_observe.sdk.utils.const import (
    ObserveSpanKindValues,
    OBSERVE_SPAN_KIND,
    OBSERVE_ENTITY_NAME,
    OBSERVE_ENTITY_VERSION,
    OBSERVE_ENTITY_INPUT,
    OBSERVE_ENTITY_OUTPUT,
)
from ioa_observe.sdk.utils.json_encoder import JSONEncoder
from ioa_observe.sdk.metrics.agent import topology_dynamism, determinism_score

P = ParamSpec("P")

R = TypeVar("R")
F = TypeVar("F", bound=Callable[..., Any])


def _is_json_size_valid(json_str: str) -> bool:
    """Check if JSON string size is less than 1MB"""
    return len(json_str) < 1_000_000


def _handle_generator(span, res):
    # for some reason the SPAN_KEY is not being set in the context of the generator, so we re-set it
    context_api.attach(trace.set_span_in_context(span))
    yield from res
    span.end()

    # Note: we don't detach the context here as this fails in some situations
    # https://github.com/open-telemetry/opentelemetry-python/issues/2606
    # This is not a problem since the context will be detached automatically during garbage collection


async def _ahandle_generator(span, ctx_token, res):
    async for part in res:
        yield part

    span.end()
    context_api.detach(ctx_token)


def _should_send_prompts():
    return (
        os.getenv("OBSERVE_TRACE_CONTENT") or "true"
    ).lower() == "true" or context_api.get_value("override_enable_content_tracing")


def _get_session_span_key(session_id: str, key_type: str) -> str:
    """Generate a kv_store key for session-scoped span tracking."""
    return f"session.{session_id}.{key_type}"


def _store_agent_span_info(
    session_id: str, span, entity_name: str, parent_seq: int = 0
) -> tuple:
    """Store current agent's span info in kv_store for next agent to link to.

    Also writes a per-sequence agent record and runs implicit fork detection.

    Args:
        session_id: Current session ID.
        span: The agent's OTel span.
        entity_name: Agent entity name.
        parent_seq: Sequence number of the agent that preceded this one (0 if first).

    Returns:
        (new_seq, fork_id, branch_index) — fork_id is "" if no fork detected.
    """
    if not session_id:
        return 0, "", -1

    span_context = span.get_span_context()
    span_id_hex = format(span_context.span_id, "016x")
    trace_id_hex = format(span_context.trace_id, "032x")

    # Update the global "last agent" cursor (backwards compat)
    kv_store.set(
        _get_session_span_key(session_id, "last_agent_span_id"),
        span_id_hex,
    )
    kv_store.set(
        _get_session_span_key(session_id, "last_agent_trace_id"),
        trace_id_hex,
    )
    kv_store.set(_get_session_span_key(session_id, "last_agent_name"), entity_name)

    # Increment agent sequence number
    sequence_key = _get_session_span_key(session_id, "agent_sequence")
    current_seq = kv_store.get(sequence_key)
    new_seq = (int(current_seq) + 1) if current_seq else 1
    kv_store.set(sequence_key, str(new_seq))

    # Run implicit fork detection and write per-sequence record
    fork_id, branch_index = detect_fork_on_agent_start(
        session_id=session_id,
        new_sequence=new_seq,
        parent_seq=parent_seq,
        span_id=span_id_hex,
        trace_id=trace_id_hex,
        entity_name=entity_name,
    )

    return new_seq, fork_id, branch_index


def _get_previous_agent_link(session_id: str) -> tuple:
    """Get link(s) to previous agent span(s) from kv_store.

    Uses the OTel span hierarchy to find the true parent agent for fork/join
    detection.  When parallel agents (fan-out) are spawned inside a parent
    agent's scope, they all share the same OTel parent span.  By looking up
    which agent record owns that span ID we get the correct parent_seq,
    enabling fork sibling detection.

    Returns:
        tuple: (links_list, previous_agent_name, sequence, join_fork_id, true_parent_seq)
    """
    if not session_id:
        return [], None, 0, None, 0

    sequence_str = kv_store.get(_get_session_span_key(session_id, "agent_sequence"))
    sequence = int(sequence_str) if sequence_str else 0

    # -----------------------------------------------------------------
    # Step 1: Check if the current OTel context has a parent span that
    # belongs to a known agent.  This correctly identifies the
    # dispatching agent in fan-out scenarios.
    # -----------------------------------------------------------------
    true_parent_seq = 0
    current_span = trace.get_current_span()
    if current_span and hasattr(current_span, "get_span_context"):
        span_ctx = current_span.get_span_context()
        if span_ctx and span_ctx.is_valid:
            parent_span_id_hex = format(span_ctx.span_id, "016x")
            true_parent_seq = find_parent_agent_seq(
                session_id, parent_span_id_hex, sequence + 1
            )

    if true_parent_seq > 0:
        # Found a registered agent as the OTel parent -> link to it.
        parent_rec = get_agent_record(session_id, true_parent_seq)
        if parent_rec and parent_rec["span_id"] and parent_rec["trace_id"]:
            links = make_single_link(
                parent_rec["span_id"],
                parent_rec["trace_id"],
                parent_rec["name"],
                "agent_handoff",
            )
            return links, parent_rec["name"], sequence, None, true_parent_seq

    # -----------------------------------------------------------------
    # Step 2: No agent parent in OTel context (e.g., new trace, root
    # agent, or join after fork completed).  Use join detection which
    # reads the "last agent" cursor.
    # -----------------------------------------------------------------
    links, join_fork_id, prev_agent_name = detect_join_and_build_links(
        session_id,
        sequence + 1,  # +1 because we haven't incremented yet
    )

    # If join detection didn't find a previous name, read from cursor
    if prev_agent_name is None and not join_fork_id:
        prev_agent_name = kv_store.get(
            _get_session_span_key(session_id, "last_agent_name")
        )

    # For sequential / join case, the "parent" is the last sequential agent
    return links, prev_agent_name, sequence, join_fork_id, sequence


def _setup_span(
    entity_name,
    tlp_span_kind: Optional[ObserveSpanKindValues] = None,
    version: Optional[int] = None,
    description: Optional[str] = None,
    application_id: Optional[str] = None,
):
    """Sets up the OpenTelemetry span and context"""
    if tlp_span_kind in [
        ObserveSpanKindValues.WORKFLOW,
        ObserveSpanKindValues.AGENT,
        "graph",
    ]:
        set_workflow_name(entity_name)
        # if tlp_span_kind == "graph":
        #     session_id = entity_name + "_" + str(uuid.uuid4())
        #     set_session_id(session_id)
    if tlp_span_kind == "graph":
        span_name = f"{entity_name}.{tlp_span_kind}"

    else:
        span_name = f"{entity_name}.{tlp_span_kind.value}"

    # Get session_id early (before creating span) for linking
    session_id_value = get_value("session.id")
    session_id: Optional[str] = str(session_id_value) if session_id_value else None

    # Get link to previous agent (for agent spans only)
    links = []
    previous_agent_name = None
    agent_sequence = 0
    join_fork_id = None
    true_parent_seq = 0

    if tlp_span_kind == ObserveSpanKindValues.AGENT and session_id:
        (
            prev_links,
            previous_agent_name,
            agent_sequence,
            join_fork_id,
            true_parent_seq,
        ) = _get_previous_agent_link(session_id)
        links.extend(prev_links)

    # Capture the current (parent) span BEFORE creating the new span.
    # This is critical for tool-level fork detection: once we create and
    # attach the new span, get_current_span() returns the NEW span.
    _pre_parent_span_hex = ""
    if tlp_span_kind == ObserveSpanKindValues.TOOL and session_id:
        _pre_parent = trace.get_current_span()
        if _pre_parent and hasattr(_pre_parent, "get_span_context"):
            _p_ctx = _pre_parent.get_span_context()
            if _p_ctx and _p_ctx.is_valid:
                _pre_parent_span_hex = format(_p_ctx.span_id, "016x")

    with get_tracer() as tracer:
        # Create span with links to previous agent
        span = tracer.start_span(span_name, links=links if links else None)
        ctx = trace.set_span_in_context(span)

        # Preserve existing context values before attaching new context
        current_traceparent = get_value("current_traceparent")
        agent_id = get_value("agent_id")
        application_id_ctx = get_value("application_id")
        association_properties = get_value("association_properties")
        managed_prompt = get_value("managed_prompt")
        prompt_key = get_value("prompt_key")
        prompt_version = get_value("prompt_version")
        prompt_version_name = get_value("prompt_version_name")
        prompt_version_hash = get_value("prompt_version_hash")
        prompt_template = get_value("prompt_template")
        prompt_template_variables = get_value("prompt_template_variables")

        ctx_token = context_api.attach(ctx)

        # Re-attach preserved context values to the new context
        if session_id is not None:
            attach(set_value("session.id", session_id))
        if current_traceparent is not None:
            attach(set_value("current_traceparent", current_traceparent))
        if agent_id is not None:
            attach(set_value("agent_id", agent_id))
        if application_id_ctx is not None:
            attach(set_value("application_id", application_id_ctx))
        if association_properties is not None:
            attach(set_value("association_properties", association_properties))
        if managed_prompt is not None:
            attach(set_value("managed_prompt", managed_prompt))
        if prompt_key is not None:
            attach(set_value("prompt_key", prompt_key))
        if prompt_version is not None:
            attach(set_value("prompt_version", prompt_version))
        if prompt_version_name is not None:
            attach(set_value("prompt_version_name", prompt_version_name))
        if prompt_version_hash is not None:
            attach(set_value("prompt_version_hash", prompt_version_hash))
        if prompt_template is not None:
            attach(set_value("prompt_template", prompt_template))
        if prompt_template_variables is not None:
            attach(set_value("prompt_template_variables", prompt_template_variables))

        if tlp_span_kind == ObserveSpanKindValues.AGENT:
            with trace.get_tracer(__name__).start_span(
                "agent_start_event", context=trace.set_span_in_context(span)
            ) as start_span:
                start_span.add_event(
                    "agent_start_event",
                    {
                        "agent_name": entity_name,
                        "description": description if description else "",
                        "type": tlp_span_kind.value,
                    },
                )
            # start_span.end()  # end the span immediately

            # Set agent sequence and previous agent info for linking
            span.set_attribute("ioa_observe.agent.sequence", agent_sequence + 1)
            if previous_agent_name:
                span.set_attribute("ioa_observe.agent.previous", previous_agent_name)

            # Store this agent's span info and run implicit fork detection
            fork_id = ""
            branch_index = -1
            if session_id:
                new_seq, fork_id, branch_index = _store_agent_span_info(
                    session_id, span, entity_name, parent_seq=true_parent_seq
                )
                # Store sequence on span object for _cleanup_span to use
                span._ioa_session_id = session_id
                span._ioa_agent_sequence = new_seq
                # Register live span ref for retroactive fork annotation
                register_active_span(session_id, new_seq, span)

            # Annotate fork attributes if this agent is a fork branch
            if fork_id:
                parent_name = previous_agent_name or "unknown"
                annotate_fork_branch(
                    span, fork_id, branch_index, true_parent_seq, parent_name
                )

            # Annotate join attributes if this agent is a join point
            if join_fork_id:
                annotate_join(span, join_fork_id, len(links))

        if tlp_span_kind in [
            ObserveSpanKindValues.TASK,
            ObserveSpanKindValues.TOOL,
            "graph",
        ]:
            entity_path = get_chained_entity_path(entity_name)
            set_entity_path(entity_path)

        # --- Tool-level fork detection (parallel tools within one agent) ---
        if (
            tlp_span_kind == ObserveSpanKindValues.TOOL
            and session_id
            and _pre_parent_span_hex
        ):
            parent_hex = _pre_parent_span_hex
            # The workflow_name in context is the parent agent's name
            _parent_agent_name = str(get_value("workflow_name") or "unknown")
            s_ctx = span.get_span_context()
            t_sid = format(s_ctx.span_id, "016x")
            t_tid = format(s_ctx.trace_id, "032x")
            tool_seq, tool_fork_id, tool_branch_idx = detect_tool_fork_on_start(
                session_id,
                parent_hex,
                span,
                entity_name,
                t_sid,
                t_tid,
                parent_agent_name=_parent_agent_name,
            )
            # Stash metadata on span for cleanup
            span._ioa_session_id = session_id
            span._ioa_tool_parent_hex = parent_hex
            span._ioa_tool_seq = tool_seq
            if tool_fork_id:
                annotate_fork_branch(
                    span,
                    tool_fork_id,
                    tool_branch_idx,
                    0,
                    _parent_agent_name,
                )

        if tlp_span_kind == "graph":
            span.set_attribute(OBSERVE_SPAN_KIND, tlp_span_kind)
        else:
            span.set_attribute(OBSERVE_SPAN_KIND, tlp_span_kind.value)
        span.set_attribute(OBSERVE_ENTITY_NAME, entity_name)
        if version:
            span.set_attribute(OBSERVE_ENTITY_VERSION, version)

        if tlp_span_kind == ObserveSpanKindValues.AGENT:
            span.set_attribute("agent_chain_start_time", time.time())
        if application_id:
            set_application_id(application_id)
            span.set_attribute(
                "application_id", application_id
            )  # set application id attribute
    return span, ctx, ctx_token


def _handle_span_input(span, args, kwargs, cls=None):
    """Handles entity input logging in JSON for both sync and async functions"""
    try:
        if _should_send_prompts():
            # Use a safer serialization approach to avoid recursion
            safe_args = []
            safe_kwargs = {}

            # Safely convert args
            for arg in args:
                try:
                    # Check if the object can be JSON serialized directly
                    json.dumps(arg)
                    safe_args.append(arg)
                except (TypeError, ValueError, PydanticSerializationError):
                    # Use intelligent serialization
                    safe_args.append(_serialize_object(arg))

            # Safely convert kwargs
            for key, value in kwargs.items():
                try:
                    # Test if the object can be JSON serialized directly
                    json.dumps(value)
                    safe_kwargs[key] = value
                except (TypeError, ValueError, PydanticSerializationError):
                    # Use intelligent serialization
                    safe_kwargs[key] = _serialize_object(value)

            # Create the JSON
            json_input = json.dumps({"args": safe_args, "kwargs": safe_kwargs})

            if _is_json_size_valid(json_input):
                span.set_attribute(
                    OBSERVE_ENTITY_INPUT,
                    json_input,
                )
    except Exception as e:
        # Log the exception but don't fail the actual function call
        print(f"Warning: Failed to serialize input for span: {e}")
        Telemetry().log_exception(e)


def _handle_span_output(span, tlp_span_kind, res, cls=None):
    """Handles entity output logging in JSON for both sync and async functions"""
    try:
        if tlp_span_kind == ObserveSpanKindValues.AGENT:
            if "agent_id" in span.attributes:
                agent_id = span.attributes["agent_id"]
                if agent_id:
                    with trace.get_tracer(__name__).start_span(
                        "agent_end_event", context=trace.set_span_in_context(span)
                    ) as end_span:
                        end_span.add_event(
                            "agent_end_event",
                            {"agent_name": agent_id, "type": "agent"},
                        )
                    # end_span.end()  # end the span immediately
                    set_agent_id_event("")  # reset the agent id event
                # Add agent interpretation scoring
        if (
            tlp_span_kind == ObserveSpanKindValues.AGENT
            or tlp_span_kind == ObserveSpanKindValues.WORKFLOW
        ):
            current_agent = span.attributes.get("agent_id", "unknown")

            # Determine next agent from response (if Command object with goto)
            next_agent = None
            if isinstance(res, dict) and "goto" in res:
                next_agent = res["goto"]
                # Check if there's an error flag in the response
                success = not (res.get("error", False) or res.get("goto") == "__end__")

                # If we have a chain of communication, compute interpretation score
                if next_agent and next_agent != "__end__":
                    score = compute_agent_interpretation_score(
                        sender_agent=current_agent,
                        receiver_agent=next_agent,
                        data=res,
                    )
                    span.set_attribute("gen_ai.ioa.agent.interpretation_score", score)
                    reliability = connection_tracker.record_connection(
                        sender=current_agent, receiver=next_agent, success=success
                    )
                    span.set_attribute(
                        "gen_ai.ioa.agent.connection_reliability", reliability
                    )

        if _should_send_prompts():
            try:
                # Try direct JSON serialization first
                json_output = json.dumps(res)
            except (TypeError, PydanticSerializationError, ValueError):
                # Use intelligent serialization for complex objects
                try:
                    serialized_res = _serialize_object(res)
                    json_output = json.dumps(serialized_res)
                except Exception:
                    # If all serialization fails, skip output attribute
                    json_output = None

            if json_output and _is_json_size_valid(json_output):
                span.set_attribute(
                    OBSERVE_ENTITY_OUTPUT,
                    json_output,
                )
                TracerWrapper().span_processor_on_ending(
                    span
                )  # record the response latency
    except Exception as e:
        print(f"Warning: Failed to serialize output for span: {e}")
        Telemetry().log_exception(e)


def _cleanup_span(span, ctx_token):
    """End the span process and detach the context token"""

    # Mark agent as no longer in-flight in the per-sequence registry
    # so that fork/join detection works correctly.
    session_id = getattr(span, "_ioa_session_id", None)
    agent_seq = getattr(span, "_ioa_agent_sequence", None)
    if session_id and agent_seq:
        mark_agent_ended(session_id, agent_seq)
        unregister_active_span(session_id, agent_seq)

    # Mark tool as no longer in-flight for tool-level fork detection
    tool_parent_hex = getattr(span, "_ioa_tool_parent_hex", None)
    tool_seq = getattr(span, "_ioa_tool_seq", None)
    if session_id and tool_parent_hex and tool_seq:
        mark_tool_ended_for_fork(session_id, tool_parent_hex, tool_seq)

    span.end()
    context_api.detach(ctx_token)


def _unwrap_structured_tool(fn):
    # Unwraps StructuredTool or similar wrappers to get the underlying function
    if hasattr(fn, "func") and callable(fn.func):
        return fn.func
    return fn


def entity_method(
    name: Optional[str] = None,
    description: Optional[str] = None,
    version: Optional[int] = None,
    protocol: Optional[str] = None,
    application_id: Optional[str] = None,
    tlp_span_kind: Optional[ObserveSpanKindValues] = ObserveSpanKindValues.TASK,
) -> Callable[[F], F]:
    def decorate(fn: F) -> F:
        # Unwrap StructuredTool if present
        fn = _unwrap_structured_tool(fn)
        is_async = _is_async_method(fn)
        entity_name = name or _get_original_function_name(fn)
        if is_async:
            if _is_async_generator(fn):

                @wraps(fn)
                async def async_gen_wrap(*args: Any, **kwargs: Any) -> Any:
                    if not TracerWrapper.verify_initialized():
                        async for item in fn(*args, **kwargs):
                            yield item
                        return

                    span, ctx, ctx_token = _setup_span(
                        entity_name,
                        tlp_span_kind,
                        version,
                        description,
                        application_id,
                    )
                    _handle_span_input(span, args, kwargs, cls=JSONEncoder)

                    async for item in _ahandle_generator(
                        span, ctx_token, fn(*args, **kwargs)
                    ):
                        yield item

                return async_gen_wrap
            else:

                @wraps(fn)
                async def async_wrap(*args, **kwargs):
                    if not TracerWrapper.verify_initialized():
                        return await fn(*args, **kwargs)

                    span, ctx, ctx_token = _setup_span(
                        entity_name,
                        tlp_span_kind,
                        version,
                        description,
                        application_id,
                    )

                    # Handle case where span setup failed
                    if span is None:
                        return fn(*args, **kwargs)
                    _handle_span_input(span, args, kwargs, cls=JSONEncoder)
                    success = False
                    try:
                        res = await fn(*args, **kwargs)
                        success = True

                        # Track successful tool call
                        if tlp_span_kind == ObserveSpanKindValues.TOOL:
                            tool_call_tracker.record_tool_call(
                                entity_name, success=True
                            )

                        # Record connection reliability for agent nodes
                        if tlp_span_kind == ObserveSpanKindValues.AGENT:
                            # Check if this agent had a previous failure
                            # If this is the first success after a failure, count it as recovery
                            with agent_recovery_tracker.lock:
                                agent_failures = (
                                    agent_recovery_tracker.agent_failures.get(
                                        entity_name, []
                                    )
                                )
                                agent_recoveries = (
                                    agent_recovery_tracker.agent_recoveries.get(
                                        entity_name, []
                                    )
                                )

                                if len(agent_failures) > len(agent_recoveries):
                                    agent_recovery_tracker.record_agent_recovery(
                                        entity_name
                                    )
                        _handle_graph_response(span, res, protocol, tlp_span_kind)
                        # span will be ended in the generator
                        if isinstance(res, types.GeneratorType):
                            return _handle_generator(span, res)
                        _handle_execution_result(span, success)

                        _handle_span_output(span, tlp_span_kind, res, cls=JSONEncoder)
                    except Exception as e:
                        span.record_exception(e)
                        span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))

                        # Track failed tool call
                        if tlp_span_kind == ObserveSpanKindValues.TOOL:
                            error_type = type(e).__name__
                            tool_call_tracker.record_tool_call(
                                entity_name, success=False, error_type=error_type
                            )

                        # Record failed connection if we know the intended recipient
                        if tlp_span_kind == ObserveSpanKindValues.AGENT:
                            error_type = type(e).__name__
                            agent_recovery_tracker.record_agent_failure(
                                entity_name, error_type
                            )
                            agent_availability.record_agent_activity(
                                entity_name, success=False
                            )
                            current_agent = entity_name

                            # Try to determine intended recipient from error context
                            reliability = connection_reliability.record_connection_attempt(
                                sender=current_agent,
                                receiver="unknown",  # Or extract from error context if possible
                                success=False,
                            )
                            span.set_attribute(
                                "gen_ai.ioa.agent.connection_reliability", reliability
                            )
                        _handle_agent_failure_event(str(e), span, tlp_span_kind)
                        raise e
                    finally:
                        if tlp_span_kind == ObserveSpanKindValues.AGENT:
                            TracerWrapper().record_agent_execution(entity_name, success)
                        _cleanup_span(span, ctx_token)
                    return res

            decorated = async_wrap
        else:

            @wraps(fn)
            def sync_wrap(*args: Any, **kwargs: Any) -> Any:
                if not TracerWrapper.verify_initialized():
                    return fn(*args, **kwargs)

                span, ctx, ctx_token = _setup_span(
                    entity_name,
                    tlp_span_kind,
                    version,
                    description,
                    application_id,
                )

                # Handle case where span setup failed
                if span is None:
                    return fn(*args, **kwargs)

                _handle_span_input(span, args, kwargs, cls=JSONEncoder)
                _handle_agent_span(span, entity_name, description, tlp_span_kind)
                success = False

                # Record heartbeat for agent
                if tlp_span_kind == ObserveSpanKindValues.AGENT:
                    agent_availability.record_agent_heartbeat(entity_name)

                # Record heartbeat for agent
                if tlp_span_kind == ObserveSpanKindValues.AGENT:
                    agent_availability.record_agent_heartbeat(entity_name)

                try:
                    res = fn(*args, **kwargs)
                    success = True

                    # Track successful tool call
                    if tlp_span_kind == ObserveSpanKindValues.TOOL:
                        tool_call_tracker.record_tool_call(entity_name, success=True)

                    # Record successful operation for agent
                    if tlp_span_kind == ObserveSpanKindValues.AGENT:
                        agent_availability.record_agent_activity(
                            entity_name, success=True
                        )

                    # Record connection reliability for agent nodes
                    if tlp_span_kind == ObserveSpanKindValues.AGENT:
                        current_agent = entity_name
                        # Check if this agent had a previous failure
                        # If this is the first success after a failure, count it as recovery
                        with agent_recovery_tracker.lock:
                            agent_failures = agent_recovery_tracker.agent_failures.get(
                                entity_name, []
                            )
                            agent_recoveries = (
                                agent_recovery_tracker.agent_recoveries.get(
                                    entity_name, []
                                )
                            )

                            if len(agent_failures) > len(agent_recoveries):
                                agent_recovery_tracker.record_agent_recovery(
                                    entity_name
                                )
                    _handle_graph_response(span, res, protocol, tlp_span_kind)

                    # span will be ended in the generator
                    if isinstance(res, types.GeneratorType):
                        return _handle_generator(span, res)
                    _handle_execution_result(span, success)
                    _handle_span_output(span, tlp_span_kind, res, cls=JSONEncoder)

                except Exception as e:
                    span.record_exception(e)
                    span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))

                    # Track failed tool call
                    if tlp_span_kind == ObserveSpanKindValues.TOOL:
                        error_type = type(e).__name__
                        tool_call_tracker.record_tool_call(
                            entity_name, success=False, error_type=error_type
                        )
                    # Record failed connection if we know the intended recipient
                    if tlp_span_kind == ObserveSpanKindValues.AGENT:
                        error_type = type(e).__name__
                        agent_recovery_tracker.record_agent_failure(
                            entity_name, error_type
                        )
                        agent_availability.record_agent_activity(
                            entity_name, success=False
                        )
                        current_agent = entity_name

                        # Try to determine intended recipient from error context
                        # This is more complex and may require additional context in your system
                        reliability = connection_reliability.record_connection_attempt(
                            sender=current_agent,
                            receiver="unknown",  # Or extract from error context if possible
                            success=False,
                        )
                        span.set_attribute(
                            "gen_ai.ioa.agent.connection_reliability", reliability
                        )
                    _handle_agent_failure_event(str(e), span, tlp_span_kind)
                    raise e
                finally:
                    if tlp_span_kind == ObserveSpanKindValues.AGENT:
                        TracerWrapper().record_agent_execution(entity_name, success)
                    _cleanup_span(span, ctx_token)
                return res

            decorated = sync_wrap
        # # If the original fn was a StructuredTool, re-wrap
        if hasattr(fn, "func") and callable(fn.func):
            fn.func = decorated
            return fn
        return decorated

    return decorate


def entity_class(
    name: Optional[str],
    description: Optional[str],
    version: Optional[int],
    protocol: Optional[str],
    application_id: Optional[str],
    method_name: Optional[str],
    tlp_span_kind: Optional[ObserveSpanKindValues] = ObserveSpanKindValues.TASK,
):
    def decorator(cls):
        task_name = name if name else camel_to_snake(cls.__qualname__)

        methods_to_wrap = []

        if method_name:
            # Specific method specified - existing behavior
            methods_to_wrap = [method_name]
        else:
            # No method specified - wrap all public methods defined in this class
            for attr_name in dir(cls):
                if (
                    not attr_name.startswith("_")  # Skip private/built-in methods
                    and attr_name != "mro"  # Skip class method
                    and hasattr(cls, attr_name)
                ):
                    attr = getattr(cls, attr_name)
                    # Only wrap functions defined in this class (not inherited methods or built-ins)
                    if (
                        inspect.isfunction(attr)  # Functions defined in the class
                        and not isinstance(attr, (classmethod, staticmethod, property))
                        and hasattr(attr, "__qualname__")  # Has qualname attribute
                        and attr.__qualname__.startswith(
                            cls.__name__ + "."
                        )  # Defined in this class
                    ):
                        # Additional check: ensure the function has a proper signature with 'self' parameter
                        try:
                            sig = inspect.signature(attr)
                            params = list(sig.parameters.keys())
                            if params and params[0] == "self":
                                methods_to_wrap.append(attr_name)
                        except (ValueError, TypeError):
                            # Skip methods that can't be inspected
                            continue

        # Wrap all detected methods
        for method_to_wrap in methods_to_wrap:
            if hasattr(cls, method_to_wrap):
                original_method = getattr(cls, method_to_wrap)
                # Only wrap actual functions defined in this class
                unwrapped_method = _unwrap_structured_tool(original_method)
                if inspect.isfunction(unwrapped_method):
                    try:
                        # Verify the method has a proper signature
                        sig = inspect.signature(unwrapped_method)
                        wrapped_method = entity_method(
                            name=f"{task_name}.{method_to_wrap}",
                            description=description,
                            version=version,
                            protocol=protocol,
                            application_id=application_id,
                            tlp_span_kind=tlp_span_kind,
                        )(unwrapped_method)
                        # Set the wrapped method on the class
                        setattr(cls, method_to_wrap, wrapped_method)
                    except Exception:
                        # Don't wrap methods that can't be properly decorated
                        continue

        return cls

    return decorator


def _handle_agent_span(span, entity_name, description, tlp_span_kind):
    if tlp_span_kind == ObserveSpanKindValues.AGENT:
        try:
            span.set_attribute("agent_id", entity_name)  # set the agent id attribute
            set_agent_id_event(entity_name)
            span.add_event(
                "agent_start_event",
                {
                    "agent_name": entity_name,
                    "description": description if description else "",
                    "type": tlp_span_kind.value
                    if tlp_span_kind != "graph"
                    else "graph",
                },
            )
        except Exception:
            traceback.print_exc()
        TracerWrapper().increment_active_agents(
            1, span
        )  # increment the number of active agents


def _handle_agent_failure_event(res, span, tlp_span_kind):
    if not span.is_recording():
        # Skip if span is already ended
        return
    if tlp_span_kind == ObserveSpanKindValues.AGENT:
        attributes = {}
        attributes["failure_reason"] = res

        if "observe.workflow.name" in span.attributes:
            attributes["agent_name"] = span.attributes["observe.workflow.name"]
        elif "traceloop.workflow.name" in span.attributes:
            attributes["agent_name"] = span.attributes["traceloop.workflow.name"]

        TracerWrapper().failing_agents_counter.add(1, attributes=attributes)


def _handle_execution_result(span, success):
    if not span.is_recording():
        # Skip if span is already ended
        return
    if success:
        span.set_attribute("execution.success", True)
    else:
        span.set_attribute("execution.success", False)
    return


def _handle_graph_response(span, res, protocol, tlp_span_kind):
    if tlp_span_kind == "graph":
        if protocol:
            protocol = protocol.upper()
            span.set_attribute("gen_ai.ioa.graph.protocol", protocol)
        # Check if the response is a Llama Index Workflow object
        graph = determine_workflow_type(res)
        if graph is not None:
            # Convert the graph to JSON string
            graph_json = json.dumps(graph, indent=2)
            span.set_attribute("gen_ai.ioa.graph", graph_json)
            # get graph dynamism
            dynamism = topology_dynamism(graph)
            span.set_attribute("gen_ai.ioa.graph_dynamism", dynamism)

            # get graph determinism score
            span.set_attribute(
                "gen_ai.ioa.graph_determinism_score", determinism_score(graph)
            )
        # Check if the response is a Langgraph CompiledStateGraph
        elif isinstance(res, CompiledStateGraph):
            # convert res object to Graph object
            s_graph = res.get_graph()
            # Convert nodes to JSON-compatible format
            graph_dict = {
                "nodes": {
                    node_id: {
                        "id": node.id,
                        "name": node.name,
                        "data": str(
                            node.data
                        ),  # Convert data to string if not serializable
                        "metadata": node.metadata,
                    }
                    for node_id, node in s_graph.nodes.items()
                },
                "edges": [
                    {
                        "source": edge.source,
                        "target": edge.target,
                        "data": str(edge.data) if edge.data is not None else None,
                        "conditional": edge.conditional,
                    }
                    for edge in s_graph.edges
                ],
            }

            # Convert to JSON string
            s_graph_json = json.dumps(graph_dict)
            # convert to JSON and set as attribute
            span.set_attribute("gen_ai.ioa.graph", s_graph_json)

            # get graph dynamism
            dynamism = topology_dynamism(graph_dict)
            span.set_attribute("gen_ai.ioa.graph_dynamism", dynamism)

            # get graph determinism score
            span.set_attribute(
                "gen_ai.ioa.graph_determinism_score", determinism_score(graph_dict)
            )
