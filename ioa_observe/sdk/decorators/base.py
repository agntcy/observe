# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import json
import traceback
from functools import wraps
import os
import types
from typing import Optional, TypeVar, Callable, Awaitable, Any, cast, Union
import inspect

from langgraph.graph.state import CompiledStateGraph
from opentelemetry import trace
from opentelemetry import context as context_api
from pydantic_core import PydanticSerializationError
from typing_extensions import ParamSpec

from ioa_observe.sdk.decorators.util import determine_workflow_type
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
F = TypeVar("F", bound=Callable[P, Union[R, Awaitable[R]]])


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


# Unified Decorators : handles both sync and async operations


def _is_async_method(fn):
    # check if co-routine function or async generator( example : using async & yield)
    return inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn)


def _setup_span(
    entity_name,
    tlp_span_kind: Optional[ObserveSpanKindValues] = None,
    version: Optional[int] = None,
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

    with get_tracer() as tracer:
        span = tracer.start_span(span_name)
        ctx = trace.set_span_in_context(span)
        ctx_token = context_api.attach(ctx)
        span.set_attribute(
            "agent_id", entity_name
        ) if tlp_span_kind == ObserveSpanKindValues.AGENT else None
        if tlp_span_kind == ObserveSpanKindValues.AGENT:
            with trace.get_tracer(__name__).start_span(
                "agent_start_event", context=trace.set_span_in_context(span)
            ) as start_span:
                start_span.add_event(
                    "agent_start_event",
                    {
                        "agent_name": entity_name,
                        "type": tlp_span_kind.value,
                    },
                )
            # start_span.end()  # end the span immediately
        # session_id = get_value("session.id")
        # if session_id is not None:
        #     span.set_attribute("session.id", session_id)
        if tlp_span_kind in [
            ObserveSpanKindValues.TASK,
            ObserveSpanKindValues.TOOL,
            "graph",
        ]:
            entity_path = get_chained_entity_path(entity_name)
            set_entity_path(entity_path)

        if tlp_span_kind == "graph":
            span.set_attribute(OBSERVE_SPAN_KIND, tlp_span_kind)
        else:
            span.set_attribute(OBSERVE_SPAN_KIND, tlp_span_kind.value)
        span.set_attribute(OBSERVE_ENTITY_NAME, entity_name)
        if version:
            span.set_attribute(OBSERVE_ENTITY_VERSION, version)

        # if session_id:
        #     print(f"Execution ID: {session_id}")
        #     span.set_attribute("session.id", session_id)

    return span, ctx, ctx_token


def _handle_span_input(span, args, kwargs, cls=None):
    """Handles entity input logging in JSON for both sync and async functions"""
    try:
        if _should_send_prompts():
            json_input = json.dumps(
                {"args": args, "kwargs": kwargs}, **({"cls": cls} if cls else {})
            )
            if _is_json_size_valid(json_input):
                span.set_attribute(
                    OBSERVE_ENTITY_INPUT,
                    json_input,
                )
    except TypeError as e:
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
                    success = not (
                        res.get("error", False) or res.get("goto") == "__end__"
                    )

                    # If we have a chain of communication, compute interpretation score
                    if next_agent and next_agent != "__end__":
                        score = compute_agent_interpretation_score(
                            sender_agent=current_agent,
                            receiver_agent=next_agent,
                            data=res,
                        )
                        span.set_attribute(
                            "gen_ai.ioa.agent.interpretation_score", score
                        )
                        reliability = connection_tracker.record_connection(
                            sender=current_agent, receiver=next_agent, success=success
                        )
                        span.set_attribute(
                            "gen_ai.ioa.agent.connection_reliability", reliability
                        )

        if _should_send_prompts():
            try:
                json_output = json.dumps(res, **({"cls": cls} if cls else {}))
            except (TypeError, PydanticSerializationError):
                # Fallback for objects that can't be directly serialized
                try:
                    # Try to serialize a string representation
                    safe_output = str(res)
                    json_output = json.dumps({"__str_representation__": safe_output})
                except Exception:
                    # If all serialization fails, skip output attribute
                    json_output = None
            if _is_json_size_valid(json_output):
                span.set_attribute(
                    OBSERVE_ENTITY_OUTPUT,
                    json_output,
                )
                TracerWrapper().span_processor_on_ending(
                    span
                )  # record the response latency
    except TypeError as e:
        Telemetry().log_exception(e)


def _cleanup_span(span, ctx_token):
    """End the span process and detach the context token"""
    span.end()
    context_api.detach(ctx_token)


def entity_method(
    name: Optional[str] = None,
    version: Optional[int] = None,
    tlp_span_kind: Optional[ObserveSpanKindValues] = ObserveSpanKindValues.TASK,
) -> Callable[[F], F]:
    def decorate(fn: F) -> F:
        is_async = _is_async_method(fn)
        entity_name = name or fn.__qualname__
        if is_async:
            if inspect.isasyncgenfunction(fn):

                @wraps(fn)
                async def async_gen_wrap(*args: Any, **kwargs: Any) -> Any:
                    if not TracerWrapper.verify_initialized():
                        async for item in fn(*args, **kwargs):
                            yield item
                        return

                    span, ctx, ctx_token = _setup_span(
                        entity_name, tlp_span_kind, version
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
                        entity_name, tlp_span_kind, version
                    )
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
                        _handle_graph_response(span, res, tlp_span_kind)
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
                        _cleanup_span(span, ctx_token)
                    return res

                return cast(F, async_wrap)
        else:

            @wraps(fn)
            def sync_wrap(*args: Any, **kwargs: Any) -> Any:
                if not TracerWrapper.verify_initialized():
                    return fn(*args, **kwargs)

                span, ctx, ctx_token = _setup_span(entity_name, tlp_span_kind, version)

                _handle_span_input(span, args, kwargs, cls=JSONEncoder)
                _handle_agent_span(span, entity_name, tlp_span_kind)
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
                    _handle_graph_response(span, res, tlp_span_kind)

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
                    _cleanup_span(span, ctx_token)
                return res

            return cast(F, sync_wrap)

    return decorate


def entity_class(
    name: Optional[str],
    version: Optional[int],
    method_name: str,
    tlp_span_kind: Optional[ObserveSpanKindValues] = ObserveSpanKindValues.TASK,
):
    def decorator(cls):
        task_name = name if name else camel_to_snake(cls.__qualname__)
        method = getattr(cls, method_name)
        setattr(
            cls,
            method_name,
            entity_method(name=task_name, version=version, tlp_span_kind=tlp_span_kind)(
                method
            ),
        )
        return cls

    return decorator


def _handle_agent_span(span, entity_name, tlp_span_kind):
    if tlp_span_kind == ObserveSpanKindValues.AGENT:
        try:
            set_agent_id_event(entity_name)
            span.add_event(
                "agent_start_event",
                {
                    "agent_name": entity_name,
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
        TracerWrapper().failing_agents_counter.add(
            1,
            attributes={
                "agent_name": span.attributes["observe.workflow.name"]
                if "observe.workflow.name" in span.attributes
                else span.attributes["traceloop.workflow.name"],
                "failure_reason": res,  # or could be "none" if you don't want to specify
            },
        )


def _handle_execution_result(span, success):
    if not span.is_recording():
        # Skip if span is already ended
        return
    if success:
        span.set_attribute("execution.success", True)
    else:
        span.set_attribute("execution.success", False)
    return


def _handle_graph_response(span, res, tlp_span_kind):
    if tlp_span_kind == "graph":
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
            s_graph_json = json.dumps(graph_dict, indent=2)
            span.set_attribute("gen_ai.ioa.graph", s_graph_json)

            # get graph dynamism
            dynamism = topology_dynamism(graph_dict)
            span.set_attribute("gen_ai.ioa.graph_dynamism", dynamism)

            # get graph determinism score
            span.set_attribute(
                "gen_ai.ioa.graph_determinism_score", determinism_score(graph_dict)
            )
