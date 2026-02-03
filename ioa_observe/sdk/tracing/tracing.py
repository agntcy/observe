# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import atexit
import contextlib
import json
import logging
import os
import re
import threading
import time
import uuid

from colorama import Fore
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as HTTPExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter as GRPCExporter,
)
from opentelemetry.metrics import Observation
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, SpanProcessor
from opentelemetry.propagators.textmap import TextMapPropagator
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.trace.export import (
    SpanExporter,
    SimpleSpanProcessor,
    BatchSpanProcessor,
)
from opentelemetry.trace import (
    get_tracer_provider,
    ProxyTracerProvider,
    SpanContext,
    TraceFlags,
    NonRecordingSpan,
)
from opentelemetry.context import get_value, attach, set_value
from opentelemetry.trace import set_span_in_context
from opentelemetry.instrumentation.threading import ThreadingInstrumentor
from opentelemetry.metrics import get_meter
from ioa_observe.sdk.metrics.agents.agent_connections import connection_reliability
from opentelemetry.semconv_ai import SpanAttributes
from ioa_observe.sdk import Telemetry
from ioa_observe.sdk.instruments import Instruments
from ioa_observe.sdk.tracing.content_allow_list import ContentAllowList
from ioa_observe.sdk.tracing.transform_span import (
    transform_json_object_configurable,
    validate_transformer_rules,
)
from ioa_observe.sdk.utils import is_notebook
from ioa_observe.sdk.client import kv_store

from ioa_observe.sdk.utils.const import (
    OBSERVE_WORKFLOW_NAME,
    OBSERVE_ASSOCIATION_PROPERTIES,
    OBSERVE_ENTITY_NAME,
    OBSERVE_PROMPT_TEMPLATE_VARIABLES,
    OBSERVE_PROMPT_TEMPLATE,
    OBSERVE_PROMPT_VERSION_NAME,
    OBSERVE_PROMPT_VERSION,
    OBSERVE_PROMPT_KEY,
    OBSERVE_PROMPT_VERSION_HASH,
    OBSERVE_PROMPT_MANAGED,
    OBSERVE_ENTITY_OUTPUT,
)
from ioa_observe.sdk.utils.package_check import is_package_installed
from typing import Callable, Dict, Optional, Set

TRACER_NAME = "ioa.observe.tracer"
APP_NAME = ""

SESSION_IDLE_TIMEOUT_SECONDS = 300  # e.g. 5 minutes
SESSION_WATCHER_INTERVAL_SECONDS = 30
MAX_PROCESSED_SPANS_SIZE = 100000  # Maximum size before cleanup


def determine_reliability_score(span):
    if "observe.entity.output" in span.attributes:
        current_agent = span.attributes["observe.workflow.name"]

        span_entity_output = span.attributes[OBSERVE_ENTITY_OUTPUT]
        # Check if the output is a dictionary
        # and contains the "goto" key
        try:
            parsed = json.loads(span_entity_output)
        except (ValueError, SyntaxError):
            # If parsing fails, it might be a string or other type
            parsed = span_entity_output
        if isinstance(parsed, dict) and "goto" in parsed:
            next_agent = parsed["goto"]

            # Record successful connection
            if next_agent and (next_agent != "__end__" or next_agent != "None"):
                reliability = connection_reliability.record_connection_attempt(
                    sender=current_agent, receiver=next_agent, success=True
                )
                span.set_attribute(
                    "gen_ai.ioa.agent.connection_reliability", reliability
                )
        else:
            parsed = json.loads(span_entity_output)
            if "__str_representation__" in parsed:
                inner = parsed["__str_representation__"]
                # Use regex to find can_handoff_to field
                match = re.search(r"can_handoff_to=([^\s]+)", inner)
                if match:
                    next_agent = match.group(1)

                    # Record successful connection
                    if next_agent and (next_agent != "__end__" or next_agent != "None"):
                        reliability = connection_reliability.record_connection_attempt(
                            sender=current_agent, receiver=next_agent, success=True
                        )
                        span.set_attribute(
                            "gen_ai.ioa.agent.connection_reliability", reliability
                        )


class TracerWrapper(object):
    resource_attributes: dict = {}
    enable_content_tracing: bool = True
    endpoint: str = None
    app_name: str = None
    headers: Dict[str, str] = {}
    __tracer_provider: TracerProvider = None
    __disabled: bool = False

    def __new__(
        cls,
        disable_batch=False,
        processor: SpanProcessor = None,
        propagator: TextMapPropagator = None,
        exporter: SpanExporter = None,
        should_enrich_metrics: bool = True,
        instruments: Optional[Set[Instruments]] = None,
        block_instruments: Optional[Set[Instruments]] = None,
        image_uploader=None,
    ) -> "TracerWrapper":
        if not hasattr(cls, "instance"):
            obj = cls.instance = super(TracerWrapper, cls).__new__(cls)
            if not TracerWrapper.endpoint:
                return obj

            # session activity tracking: {session_id: (last_activity_time, trace_id)}
            obj._session_last_activity: dict[str, tuple[float, int]] = {}
            obj._session_lock = threading.Lock()
            # Track sessions that have already been ended to prevent duplicates
            obj._ended_sessions: set[str] = set()
            obj._ended_sessions_lock = threading.Lock()
            obj.__image_uploader = image_uploader
            # {(agent_name): [success_count, total_count]}
            obj._agent_execution_counts = {}
            obj._agent_execution_counts_lock = threading.Lock()
            # Track spans that have been processed to avoid duplicates
            obj._processed_spans = set()
            obj._processed_spans_lock = threading.Lock()
            TracerWrapper.app_name = TracerWrapper.resource_attributes.get(
                "service.name", "observe"
            )
            obj.__resource = Resource(attributes=TracerWrapper.resource_attributes)
            obj.__tracer_provider = init_tracer_provider(resource=obj.__resource)
            if processor:
                Telemetry().capture("tracer:init", {"processor": "custom"})
                obj.__spans_processor: SpanProcessor = processor
                obj.__spans_processor_original_on_start = processor.on_start
                obj.__spans_processor_original_on_end = processor.on_end
            else:
                if exporter:
                    Telemetry().capture(
                        "tracer:init",
                        {
                            "exporter": "custom",
                            "processor": "simple" if disable_batch else "batch",
                        },
                    )
                else:
                    Telemetry().capture(
                        "tracer:init",
                        {
                            "exporter": TracerWrapper.endpoint,
                            "processor": "simple" if disable_batch else "batch",
                        },
                    )

                obj.__spans_exporter: SpanExporter = (
                    exporter
                    if exporter
                    else init_spans_exporter(
                        TracerWrapper.endpoint, TracerWrapper.headers
                    )
                )
                if disable_batch or is_notebook():
                    obj.__spans_processor: SpanProcessor = SimpleSpanProcessor(
                        obj.__spans_exporter
                    )
                else:
                    obj.__spans_processor: SpanProcessor = BatchSpanProcessor(
                        obj.__spans_exporter,
                        max_export_batch_size=128,
                        schedule_delay_millis=5000,
                    )
                obj.__spans_processor_original_on_start = None
                obj.__spans_processor_original_on_end = obj.__spans_processor.on_end

            obj.__spans_processor.on_start = obj._span_processor_on_start
            obj.__spans_processor.on_end = obj.span_processor_on_ending
            obj.__tracer_provider.add_span_processor(obj.__spans_processor)
            # Custom metric, for example
            meter = get_meter("observe")
            obj.llm_call_counter = meter.create_counter(
                name="llm_call_count",
                description="Counts the number of LLM calls",
                unit="1",
            )
            obj.number_active_agents = meter.create_counter(
                name="number_active_agents",
                description="Number of active agents",
                unit="1",
            )
            obj.failing_agents_counter = meter.create_counter(
                name="common_failing_agents",
                description="Counts agent failures by agent and reason",
                unit="1",
            )
            # obj.response_latency_histogram = meter.create_histogram(
            #     name="response_latency",
            #     description="Records the latency of responses",
            #     unit="ms",
            # )
            obj.messages_received_counter = meter.create_counter(
                "slim.messages.received",
                description="Number of SLIM messages received per agent",
            )
            obj.messages_published_counter = meter.create_counter(
                "slim.messages.published",
                description="Number of SLIM messages published per agent",
            )
            obj.active_connections_counter = meter.create_up_down_counter(
                "slim.connections.active",
                description="Number of active SLIM connections",
            )
            obj.processed_messages_counter = meter.create_counter(
                "slim.messages.processed",
                description="Number of SLIM messages processed",
            )
            # obj.processing_time = meter.create_histogram(
            #     "slim.message.processing_time",
            #     description="Time taken to process SLIM messages",
            # )
            obj.throughput_counter = meter.create_counter(
                "slim.message.throughput",
                description="Message throughput for SLIM operations",
            )
            obj.error_counter = meter.create_counter(
                "slim.errors", description="Number of SLIM message errors or drops"
            )
            # obj.agent_chain_completion_time_histogram = meter.create_histogram(
            #     name="gen_ai.client.ioa.agent.end_to_end_chain_completion_time",
            #     description="Records the end-to-end chain completion time for a single agent",
            #     unit="s",
            # )
            obj.agent_execution_success_rate = meter.create_observable_gauge(
                name="gen_ai.client.ioa.agent.execution_success_rate",
                description="Success rate of agent executions",
                unit="1",
                callbacks=[obj._observe_agent_execution_success_rate],
            )
            if propagator:
                set_global_textmap(propagator)

            # this makes sure otel context is propagated so we always want it
            ThreadingInstrumentor().instrument()

            instrument_set = init_instrumentations(
                should_enrich_metrics,
                image_uploader,
                instruments,
                block_instruments,
            )

            if not instrument_set:
                print(
                    Fore.RED + "Warning: No valid instruments set. Remove 'instrument' "
                    "argument to use all instruments, or set a valid instrument."
                )
                print(Fore.RESET)

            obj.__content_allow_list = ContentAllowList()

            # start background watcher for session end
            obj._start_session_watcher()

            # Force flushes for debug environments (e.g. local development)
            atexit.register(obj.exit_handler)

        return cls.instance

    def _start_session_watcher(self) -> None:
        t = threading.Thread(
            target=self._session_watcher_loop,
            name="ioa-session-watcher",
            daemon=True,
        )
        t.start()

    def _session_watcher_loop(self) -> None:
        while True:
            time.sleep(SESSION_WATCHER_INTERVAL_SECONDS)
            now = time.time()
            expired: dict[str, tuple[float, int]] = {}

            # Find idle sessions and remove them from _session_last_activity
            with self._session_lock:
                for session_id, (last_ts, trace_id) in list(
                    self._session_last_activity.items()
                ):
                    if now - last_ts > SESSION_IDLE_TIMEOUT_SECONDS:
                        expired[session_id] = (last_ts, trace_id)
                        del self._session_last_activity[session_id]

            # Periodic cleanup of processed spans to prevent unbounded memory growth
            with self._processed_spans_lock:
                if len(self._processed_spans) > MAX_PROCESSED_SPANS_SIZE:
                    self._processed_spans.clear()

            # Periodic cleanup of ended sessions to prevent unbounded memory growth
            with self._ended_sessions_lock:
                if len(self._ended_sessions) > MAX_PROCESSED_SPANS_SIZE:
                    self._ended_sessions.clear()

            if not expired:
                continue

            tracer = self.get_tracer()

            # Iterate over a snapshot and do *not* modify `expired` in the loop
            for session_id, (last_ts, trace_id) in list(expired.items()):
                # Check if this session has already been ended to prevent duplicates
                with self._ended_sessions_lock:
                    if session_id in self._ended_sessions:
                        continue
                    self._ended_sessions.add(session_id)

                # Create a parent context from the stored trace_id to keep session.end under the same trace
                parent_span_context = SpanContext(
                    trace_id=trace_id,
                    span_id=0,  # Use 0 as we don't have a specific parent span
                    is_remote=False,
                    trace_flags=TraceFlags(TraceFlags.SAMPLED),
                )
                parent_span = NonRecordingSpan(parent_span_context)
                parent_context = set_span_in_context(parent_span)

                with tracer.start_as_current_span(
                    "session.end", context=parent_context
                ) as span:
                    span.set_attribute("session.id", session_id)
                    workflow_name = get_value("workflow_name")
                    if workflow_name:
                        span.set_attribute(OBSERVE_WORKFLOW_NAME, workflow_name)
                    span.set_attribute("session.ended_at", last_ts)

            # ensure end spans are exported reasonably fast
            self.flush()

    def exit_handler(self):
        # emit end spans for any sessions that never went idle
        now = time.time()
        tracer = self.get_tracer()

        with self._session_lock:
            remaining_sessions = dict(self._session_last_activity)
            self._session_last_activity.clear()

        for session_id, (_, trace_id) in remaining_sessions.items():
            # Check if this session has already been ended to prevent duplicates
            with self._ended_sessions_lock:
                if session_id in self._ended_sessions:
                    continue
                self._ended_sessions.add(session_id)

            # Create a parent context from the stored trace_id to keep session.end under the same trace
            parent_span_context = SpanContext(
                trace_id=trace_id,
                span_id=0,  # Use 0 as we don't have a specific parent span
                is_remote=False,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
            parent_span = NonRecordingSpan(parent_span_context)
            parent_context = set_span_in_context(parent_span)

            with tracer.start_as_current_span(
                "session.end", context=parent_context
            ) as span:
                span.set_attribute("session.id", session_id)
                workflow_name = get_value("workflow_name")
                if workflow_name:
                    span.set_attribute(OBSERVE_WORKFLOW_NAME, workflow_name)
                span.set_attribute("session.ended_at", now)

        self.flush()

    def _span_processor_on_start(self, span, parent_context):
        workflow_name = get_value("workflow_name")
        if workflow_name is not None:
            span.set_attribute(OBSERVE_WORKFLOW_NAME, workflow_name)

        session_id = get_value("session.id")
        if session_id is not None:
            span.set_attribute("session.id", session_id)

        agent_id = get_value("agent_id")
        if agent_id is not None:
            span.set_attribute("agent_id", agent_id)

        application_id = get_value("application_id")
        if application_id is not None:
            span.set_attribute("application_id", application_id)

        if is_llm_span(span):
            self.llm_call_counter.add(1, attributes=span.attributes)

        span.set_attribute("ioa_start_time", time.time())  # Record start time

        association_properties = get_value("association_properties")
        if association_properties is not None:
            _set_association_properties_attributes(span, association_properties)

            if not self.enable_content_tracing:
                if self.__content_allow_list.is_allowed(association_properties):
                    attach(set_value("override_enable_content_tracing", True))
                else:
                    attach(set_value("override_enable_content_tracing", False))

        if is_llm_span(span):
            managed_prompt = get_value("managed_prompt")
            if managed_prompt is not None:
                span.set_attribute(OBSERVE_PROMPT_MANAGED, managed_prompt)

            prompt_key = get_value("prompt_key")
            if prompt_key is not None:
                span.set_attribute(OBSERVE_PROMPT_KEY, prompt_key)

            prompt_version = get_value("prompt_version")
            if prompt_version is not None:
                span.set_attribute(OBSERVE_PROMPT_VERSION, prompt_version)

            prompt_version_name = get_value("prompt_version_name")
            if prompt_version_name is not None:
                span.set_attribute(OBSERVE_PROMPT_VERSION_NAME, prompt_version_name)

            prompt_version_hash = get_value("prompt_version_hash")
            if prompt_version_hash is not None:
                span.set_attribute(OBSERVE_PROMPT_VERSION_HASH, prompt_version_hash)

            prompt_template = get_value("prompt_template")
            if prompt_template is not None:
                span.set_attribute(OBSERVE_PROMPT_TEMPLATE, prompt_template)

            prompt_template_variables = get_value("prompt_template_variables")
            if prompt_template_variables is not None:
                for key, value in prompt_template_variables.items():
                    span.set_attribute(
                        f"{OBSERVE_PROMPT_TEMPLATE_VARIABLES}.{key}",
                        value,
                    )

        # Call original on_start method if it exists in custom processor
        if self.__spans_processor_original_on_start:
            self.__spans_processor_original_on_start(span, parent_context)

    def increment_active_agents(self, count: int, span):
        self.number_active_agents.add(count, attributes=span.attributes)

    def span_processor_on_ending(self, span):
        # Check if this span has already been processed to avoid duplicate processing
        # Added for avoid duplicate on_ending with manual triggers
        # from decorators (@tool, @workflow) in base.py
        span_id = span.context.span_id
        with self._processed_spans_lock:
            if span_id in self._processed_spans:
                # This span was already processed, skip to avoid duplicates
                return

            # Mark this span as processed
            self._processed_spans.add(span_id)

        # update last activity per session
        # Skip session.end spans to avoid infinite loop - these are cleanup spans
        # that should not re-register the session as active
        session_id = span.attributes.get("session.id")
        if session_id and span.name != "session.end":
            with self._session_lock:
                # Store both the last activity time and the trace_id for this session
                self._session_last_activity[session_id] = (
                    time.time(),
                    span.context.trace_id,
                )

        determine_reliability_score(span)
        # start_time = span.attributes.get("ioa_start_time")

        # Apply transformations if enabled
        apply_transform = get_value("apply_transform")
        if apply_transform:
            transformer_rules = get_value("transformer_rules")
            if transformer_rules:
                try:
                    # Convert span to dict for transformation
                    span_dict = self._span_to_dict(span)
                    # Apply transformation
                    transformed_span_dict = transform_json_object_configurable(
                        span_dict, transformer_rules
                    )
                    # Update span with transformed data
                    self._update_span_from_dict(span, transformed_span_dict)
                except Exception as e:
                    logging.error(f"Error applying span transformation: {e}")

        # Call original on_end method if it exists
        if (
            hasattr(self, "_TracerWrapper__spans_processor_original_on_end")
            and self.__spans_processor_original_on_end
        ):
            self.__spans_processor_original_on_end(span)

    def _span_to_dict(self, span):
        """Convert span to dictionary for transformation."""
        span_dict = {
            "name": span.name,
            "attributes": dict(span.attributes) if span.attributes else {},
            "status": {
                "status_code": span.status.status_code.name
                if span.status and span.status.status_code
                else None,
                "description": span.status.description if span.status else None,
            },
        }
        return span_dict

    def _update_span_from_dict(self, span, span_dict):
        """Update span with transformed data."""
        # Update span name if it was transformed
        if "name" in span_dict and span_dict["name"] != span.name:
            # Directly modify the internal name attribute
            if hasattr(span, "_name"):
                span._name = span_dict["name"]

        # Update attributes if they were transformed
        if "attributes" in span_dict:
            # Try multiple approaches to update span attributes
            updated = False

            # Method 1: Try using set_attribute if available and mutable
            if (
                hasattr(span, "set_attribute")
                and hasattr(span, "_ended")
                and not span._ended
            ):
                try:
                    # Clear existing attributes by setting them to None
                    if hasattr(span, "_attributes"):
                        keys_to_remove = list(span._attributes.keys())
                        for key in keys_to_remove:
                            span.set_attribute(key, None)

                    # Set new attributes
                    for key, value in span_dict["attributes"].items():
                        span.set_attribute(key, value)
                    updated = True
                except (AttributeError, TypeError):
                    pass

            # Method 2: Direct attribute manipulation
            if not updated:
                try:
                    if hasattr(span, "_attributes"):
                        span._attributes.clear()
                        span._attributes.update(span_dict["attributes"])
                        updated = True
                    elif hasattr(span, "attributes") and hasattr(
                        span.attributes, "clear"
                    ):
                        span.attributes.clear()
                        span.attributes.update(span_dict["attributes"])
                        updated = True
                except (AttributeError, TypeError):
                    pass

            if not updated:
                logging.warning("Cannot modify span attributes - span may be finalized")

    @staticmethod
    def set_static_params(
        resource_attributes: dict,
        enable_content_tracing: bool,
        endpoint: str,
        headers: Dict[str, str],
    ) -> None:
        TracerWrapper.resource_attributes = resource_attributes
        TracerWrapper.enable_content_tracing = enable_content_tracing
        TracerWrapper.endpoint = endpoint
        TracerWrapper.headers = headers

    @classmethod
    def verify_initialized(cls) -> bool:
        if cls.__disabled:
            return False

        if hasattr(cls, "instance"):
            return True

        if (os.getenv("OBSERVE_SUPPRESS_WARNINGS") or "false").lower() == "true":
            return False

        print(
            Fore.RED
            + "Warning: observe not initialized, make sure you call observe.init()"
        )
        print(Fore.RESET)
        return False

    @classmethod
    def set_disabled(cls, disabled: bool) -> None:
        cls.__disabled = disabled

    def flush(self):
        self.__spans_processor.force_flush()

    def get_tracer(self):
        return self.__tracer_provider.get_tracer(TRACER_NAME)

    def record_agent_execution(self, agent_name: str, success: bool):
        with self._agent_execution_counts_lock:
            counts = self._agent_execution_counts.setdefault(agent_name, [0, 0])
            if success:
                counts[0] += 1  # success count
            counts[1] += 1  # total count

    def _observe_agent_execution_success_rate(self, observer):
        measurements = []
        with self._agent_execution_counts_lock:
            for agent_name, (
                success_count,
                total_count,
            ) in self._agent_execution_counts.items():
                rate = (success_count / total_count) if total_count > 0 else 0.0
                measurements.append(
                    Observation(value=rate, attributes={"agent_name": agent_name})
                )
        return measurements


def set_association_properties(properties: dict) -> None:
    attach(set_value("association_properties", properties))

    # Attach association properties to the current span, if it's a workflow or a task
    span = trace.get_current_span()
    if get_value("workflow_name") is not None or get_value("entity_name") is not None:
        _set_association_properties_attributes(span, properties)


def _set_association_properties_attributes(span, properties: dict) -> None:
    for key, value in properties.items():
        span.set_attribute(f"{OBSERVE_ASSOCIATION_PROPERTIES}.{key}", value)


def set_workflow_name(workflow_name: str) -> None:
    attach(set_value("workflow_name", workflow_name))


def _parse_boolean_env(env_value: str) -> bool:
    """
    Parse boolean value from environment variable string.

    Args:
        env_value (str): Environment variable value to parse

    Returns:
        bool: Parsed boolean value

    Accepts: "0", "1", "true", "false", "True", "False"
    """
    if env_value.lower() in ("true", "1"):
        return True
    elif env_value.lower() in ("false", "0"):
        return False
    else:
        raise ValueError(f"Invalid boolean value: {env_value}")


def session_start(apply_transform: bool = False):
    """
    Can be used as a context manager or a normal function.
    As a context manager, yields session metadata.
    As a normal function, just sets up the session.

    Args:
        apply_transform (bool): If True, enables span transformation based on
                               rules loaded from SPAN_TRANSFORMER_RULES_FILE env.
                               Can be overridden by
                               SPAN_TRANSFORMER_RULES_ENABLED env var.
    """
    session_id = (TracerWrapper.app_name or "observe") + "_" + str(uuid.uuid4())
    set_session_id(session_id)

    # Initialize span tracking for this session (for agent linking)
    kv_store.set(f"session.{session_id}.agent_sequence", "0")
    kv_store.set(f"session.{session_id}.started_at", str(time.time()))

    # Check if environment variable overrides the apply_transform parameter
    transformer_enabled_env = os.getenv("SPAN_TRANSFORMER_RULES_ENABLED")
    if transformer_enabled_env:
        try:
            apply_transform = _parse_boolean_env(transformer_enabled_env)
        except ValueError as e:
            logging.error(
                "Invalid SPAN_TRANSFORMER_RULES_ENABLED value: "
                f"{e}. Using parameter value: {apply_transform}"
            )

    # Handle transformation flag
    if apply_transform:
        transformer_rules_file = os.getenv("SPAN_TRANSFORMER_RULES_FILE")
        if not transformer_rules_file:
            logging.error(
                "SPAN_TRANSFORMER_RULES_FILE environment variable "
                "not set. Disabling transformation."
            )
            apply_transform = False
        elif not os.path.exists(transformer_rules_file):
            logging.error(
                "Transformer rules file not found: "
                f"{transformer_rules_file}. Disabling "
                "transformation."
            )
            apply_transform = False
        else:
            try:
                with open(transformer_rules_file, "r") as f:
                    transformer_rules = json.load(f)
                # Validate structure and rules
                validate_transformer_rules(transformer_rules)
                attach(set_value("apply_transform", True))
                attach(set_value("transformer_rules", transformer_rules))
            except json.JSONDecodeError as e:
                logging.error(
                    "Failed to load transformer rules from "
                    f"{transformer_rules_file}: {e}. Disabling "
                    "transformation."
                )
                apply_transform = False
            except ValueError:
                logging.error(
                    "Invalid transformer rules structure. "
                    "Expected 'RULES' section. "
                    "Disabling transformation."
                )
                apply_transform = False
            except (json.JSONDecodeError, Exception) as e:
                logging.error(
                    "Failed to load transformer rules from "
                    f"{transformer_rules_file}: {e}. "
                    "Disabling transformation."
                )
                apply_transform = False

    if not apply_transform:
        attach(set_value("apply_transform", False))

    metadata = {
        "executionID": get_value("session.id") or session_id,
        "traceparentID": get_current_traceparent(),
    }
    import inspect

    frame = inspect.currentframe().f_back
    if frame and "__enter__" in frame.f_code.co_names:
        # Used as a context manager
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield metadata

        return _cm()
    # Used as a normal function
    return contextlib.nullcontext(metadata)


def set_session_id(session_id: str, traceparent: str = None) -> None:
    """
    Sets the execution ID in both the key-value store and OpenTelemetry context.

    This function stores the execution ID with traceparent context to ensure
    proper trace correlation across distributed systems.

    Args:
        session_id: The execution ID to set
        traceparent: Optional traceparent to use (if None, will extract from context)
    """
    if not session_id:
        return

    from opentelemetry import trace
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    # If traceparent is provided (e.g., from incoming message), use it
    if traceparent:
        # Store execution ID with provided traceparent
        kv_key = f"execution.{traceparent}"
        if kv_store.get(kv_key) is None:
            kv_store.set(kv_key, session_id)

        # Store in OpenTelemetry context
        attach(set_value("session.id", session_id))
        attach(set_value("current_traceparent", traceparent))
        return

    # Check if we have an active span first
    current_span = trace.get_current_span()
    extracted_traceparent = None

    if current_span.is_recording():
        # We have an active span, use its context
        carrier = {}
        TraceContextTextMapPropagator().inject(carrier)
        extracted_traceparent = carrier.get("traceparent")
        # Store execution ID with traceparent as key
        if extracted_traceparent:
            kv_key = f"execution.{extracted_traceparent}"
            if kv_store.get(kv_key) is None:
                kv_store.set(kv_key, session_id)
    # If there is no active span, we do nothing now and when we need the traceparent
    # e.g. when propagating context, we update the kv_store with the session_id then

    # Finally, attach to context session.id and current_traceparent if present
    attach(set_value("session.id", session_id))
    if extracted_traceparent:
        attach(set_value("current_traceparent", extracted_traceparent))


def get_current_traceparent() -> str:
    """
    Get the current traceparent in a consistent way across the application.

    Returns:
        The current traceparent string, or None if no trace context exists
    """
    from opentelemetry import trace
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    # First check if we have it stored in context
    stored_traceparent = get_value("current_traceparent")
    if stored_traceparent:
        return stored_traceparent

    # Otherwise extract from current span
    current_span = trace.get_current_span()
    if current_span.is_recording():
        carrier = {}
        TraceContextTextMapPropagator().inject(carrier)
        return carrier.get("traceparent")

    return None


def set_agent_id_event(agent_id: str) -> None:
    attach(set_value("agent_id", agent_id))


def set_application_id(app_id: str) -> None:
    attach(set_value("application_id", app_id))


def set_entity_path(entity_path: str) -> None:
    attach(set_value("entity_path", entity_path))


def get_chained_entity_path(entity_name: str) -> str:
    parent = get_value("entity_path")
    if parent is None:
        return entity_name
    else:
        return f"{parent}.{entity_name}"


def set_managed_prompt_tracing_context(
    key: str,
    version: int,
    version_name: str,
    version_hash: str,
    template_variables: dict,
) -> None:
    attach(set_value("managed_prompt", True))
    attach(set_value("prompt_key", key))
    attach(set_value("prompt_version", version))
    attach(set_value("prompt_version_name", version_name))
    attach(set_value("prompt_version_hash", version_hash))
    attach(set_value("prompt_template_variables", template_variables))


def set_external_prompt_tracing_context(
    template: str, variables: dict, version: int
) -> None:
    attach(set_value("managed_prompt", False))
    attach(set_value("prompt_version", version))
    attach(set_value("prompt_template", template))
    attach(set_value("prompt_template_variables", variables))


def is_llm_span(span) -> bool:
    return span.attributes.get(SpanAttributes.LLM_REQUEST_TYPE) is not None


def init_spans_exporter(api_endpoint: str, headers: Dict[str, str]) -> SpanExporter:
    if api_endpoint and (
        "http" in api_endpoint.lower() or "https" in api_endpoint.lower()
    ):
        return HTTPExporter(
            endpoint=f"{api_endpoint}/v1/traces",
            headers=headers,
            compression=Compression.Gzip,
        )
    elif api_endpoint:
        return GRPCExporter(endpoint=f"{api_endpoint}", headers=headers)
    else:
        # Default to HTTP exporter with localhost when endpoint is None
        return HTTPExporter(
            endpoint="http://localhost:4318/v1/traces",
            headers=headers,
            compression=Compression.Gzip,
        )


def init_tracer_provider(resource: Resource) -> TracerProvider:
    provider: TracerProvider = None
    default_provider: TracerProvider = get_tracer_provider()

    if isinstance(default_provider, ProxyTracerProvider):
        provider = TracerProvider(resource=resource)
        trace.set_tracer_provider(provider)
    elif not hasattr(default_provider, "add_span_processor"):
        logging.error(
            "Cannot add span processor to the default provider since it doesn't support it"
        )
        return
    else:
        provider = default_provider
    return provider


def init_instrumentations(
    should_enrich_metrics: bool,
    base64_image_uploader: Callable[[str, str, str], str],
    instruments: Optional[Set[Instruments]] = None,
    block_instruments: Optional[Set[Instruments]] = None,
):
    block_instruments = block_instruments or set()
    instruments = instruments or set(
        Instruments
    )  # Use all instruments if none specified

    # Remove any instruments that were explicitly blocked
    instruments = instruments - block_instruments

    instrument_set = False
    for instrument in instruments:
        if instrument == Instruments.ANTHROPIC:
            if init_anthropic_instrumentor(
                should_enrich_metrics, base64_image_uploader
            ):
                instrument_set = True
        elif instrument == Instruments.BEDROCK:
            if init_bedrock_instrumentor(should_enrich_metrics):
                instrument_set = True
        elif instrument == Instruments.CREW:
            if init_crewai_instrumentor():
                instrument_set = True
        elif instrument == Instruments.GOOGLE_GENERATIVEAI:
            if init_google_generativeai_instrumentor():
                instrument_set = True
        elif instrument == Instruments.GROQ:
            if init_groq_instrumentor():
                instrument_set = True
        elif instrument == Instruments.LANGCHAIN:
            if init_langchain_instrumentor():
                instrument_set = True
        elif instrument == Instruments.LLAMA_INDEX:
            if init_llama_index_instrumentor():
                instrument_set = True
        elif instrument == Instruments.MISTRAL:
            if init_mistralai_instrumentor():
                instrument_set = True
        elif instrument == Instruments.OLLAMA:
            if init_ollama_instrumentor():
                instrument_set = True
        elif instrument == Instruments.OPENAI:
            if init_openai_instrumentor(should_enrich_metrics, base64_image_uploader):
                instrument_set = True
        elif instrument == Instruments.REQUESTS:
            if init_requests_instrumentor():
                instrument_set = True
        elif instrument == Instruments.SAGEMAKER:
            if init_sagemaker_instrumentor(should_enrich_metrics):
                instrument_set = True
        elif instrument == Instruments.TOGETHER:
            if init_together_instrumentor():
                instrument_set = True
        elif instrument == Instruments.TRANSFORMERS:
            if init_transformers_instrumentor():
                instrument_set = True
        elif instrument == Instruments.URLLIB3:
            if init_urllib3_instrumentor():
                instrument_set = True
        elif instrument == Instruments.VERTEXAI:
            if init_vertexai_instrumentor():
                instrument_set = True
        else:
            print(Fore.RED + f"Warning: {instrument} instrumentation does not exist.")
            print(
                "Usage:\n"
                "from sdk.instruments import Instruments\n"
                "observe.init(app_name='...', instruments=set([Instruments.OPENAI]))"
            )
            print(Fore.RESET)

    if not instrument_set:
        print(
            Fore.RED
            + "Warning: No valid instruments set. "
            + "Specify instruments or remove 'instruments' argument to use all instruments."
        )
        print(Fore.RESET)

    return instrument_set


def init_openai_instrumentor(
    should_enrich_metrics: bool, base64_image_uploader: Callable[[str, str, str], str]
):
    try:
        if is_package_installed("openai"):
            Telemetry().capture("instrumentation:openai:init")
            from opentelemetry.instrumentation.openai import OpenAIInstrumentor

            instrumentor = OpenAIInstrumentor(
                exception_logger=lambda e: Telemetry().log_exception(e),
                enrich_assistant=should_enrich_metrics,
                enrich_token_usage=should_enrich_metrics,
                get_common_metrics_attributes=metrics_common_attributes,
                upload_base64_image=base64_image_uploader,
            )
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        return True

    except Exception as e:
        logging.error(f"Error initializing OpenAI instrumentor: {e}")
        Telemetry().log_exception(e)
        return False


def init_anthropic_instrumentor(
    should_enrich_metrics: bool, base64_image_uploader: Callable[[str, str, str], str]
):
    try:
        if is_package_installed("anthropic"):
            Telemetry().capture("instrumentation:anthropic:init")
            from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor

            instrumentor = AnthropicInstrumentor(
                exception_logger=lambda e: Telemetry().log_exception(e),
                enrich_token_usage=should_enrich_metrics,
                get_common_metrics_attributes=metrics_common_attributes,
                upload_base64_image=base64_image_uploader,
            )
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        return True
    except Exception as e:
        logging.error(f"Error initializing Anthropic instrumentor: {e}")
        Telemetry().log_exception(e)
        return False


def init_cohere_instrumentor():
    try:
        if is_package_installed("cohere"):
            Telemetry().capture("instrumentation:cohere:init")
            from opentelemetry.instrumentation.cohere import CohereInstrumentor

            instrumentor = CohereInstrumentor(
                exception_logger=lambda e: Telemetry().log_exception(e),
            )
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        return True
    except Exception as e:
        logging.error(f"Error initializing Cohere instrumentor: {e}")
        Telemetry().log_exception(e)
        return False


def init_google_generativeai_instrumentor():
    try:
        if is_package_installed("google-generativeai"):
            Telemetry().capture("instrumentation:gemini:init")
            from opentelemetry.instrumentation.google_generativeai import (
                GoogleGenerativeAiInstrumentor,
            )

            instrumentor = GoogleGenerativeAiInstrumentor(
                exception_logger=lambda e: Telemetry().log_exception(e),
            )
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        return True
    except Exception as e:
        logging.error(f"Error initializing Gemini instrumentor: {e}")
        Telemetry().log_exception(e)
        return False


def init_langchain_instrumentor():
    try:
        if is_package_installed("langchain") or is_package_installed("langgraph"):
            Telemetry().capture("instrumentation:langchain:init")
            from opentelemetry.instrumentation.langchain import LangchainInstrumentor

            instrumentor = LangchainInstrumentor(
                exception_logger=lambda e: Telemetry().log_exception(e),
            )
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        return True
    except Exception as e:
        logging.error(f"Error initializing LangChain instrumentor: {e}")
        Telemetry().log_exception(e)
        return False


def init_mistralai_instrumentor():
    try:
        if is_package_installed("mistralai"):
            Telemetry().capture("instrumentation:mistralai:init")
            from opentelemetry.instrumentation.mistralai import MistralAiInstrumentor

            instrumentor = MistralAiInstrumentor(
                exception_logger=lambda e: Telemetry().log_exception(e),
            )
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        return True
    except Exception as e:
        logging.error(f"Error initializing MistralAI instrumentor: {e}")
        Telemetry().log_exception(e)
        return False


def init_ollama_instrumentor():
    try:
        if is_package_installed("ollama"):
            Telemetry().capture("instrumentation:ollama:init")
            from opentelemetry.instrumentation.ollama import OllamaInstrumentor

            instrumentor = OllamaInstrumentor(
                exception_logger=lambda e: Telemetry().log_exception(e),
            )
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        return True
    except Exception as e:
        logging.error(f"Error initializing Ollama instrumentor: {e}")
        Telemetry().log_exception(e)
        return False


def init_transformers_instrumentor():
    try:
        if is_package_installed("transformers"):
            Telemetry().capture("instrumentation:transformers:init")
            from opentelemetry.instrumentation.transformers import (
                TransformersInstrumentor,
            )

            instrumentor = TransformersInstrumentor(
                exception_logger=lambda e: Telemetry().log_exception(e),
            )
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        return True
    except Exception as e:
        logging.error(f"Error initializing Transformers instrumentor: {e}")
        Telemetry().log_exception(e)
        return False


def init_together_instrumentor():
    try:
        if is_package_installed("together"):
            Telemetry().capture("instrumentation:together:init")
            from opentelemetry.instrumentation.together import TogetherAiInstrumentor

            instrumentor = TogetherAiInstrumentor(
                exception_logger=lambda e: Telemetry().log_exception(e),
            )
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        return True
    except Exception as e:
        logging.error(f"Error initializing TogetherAI instrumentor: {e}")
        Telemetry().log_exception(e)
        return False


def init_llama_index_instrumentor():
    try:
        if (
            is_package_installed("llama-index")
            or is_package_installed("llama_index")
            or is_package_installed("llama-index-core")
        ):
            Telemetry().capture("instrumentation:llamaindex:init")
            from opentelemetry.instrumentation.llamaindex import LlamaIndexInstrumentor

            instrumentor = LlamaIndexInstrumentor(
                exception_logger=lambda e: Telemetry().log_exception(e),
            )
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        return True
    except Exception as e:
        logging.error(f"Error initializing LlamaIndex instrumentor: {e}")
        Telemetry().log_exception(e)
        return False


def init_requests_instrumentor():
    try:
        if is_package_installed("requests"):
            from opentelemetry.instrumentation.requests import RequestsInstrumentor

            instrumentor = RequestsInstrumentor()
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        return True
    except Exception as e:
        logging.error(f"Error initializing Requests instrumentor: {e}")
        Telemetry().log_exception(e)
        return False


def init_urllib3_instrumentor():
    try:
        if is_package_installed("urllib3"):
            from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor

            instrumentor = URLLib3Instrumentor()
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        return True
    except Exception as e:
        logging.error(f"Error initializing urllib3 instrumentor: {e}")
        Telemetry().log_exception(e)
        return False


def init_bedrock_instrumentor(should_enrich_metrics: bool):
    try:
        if is_package_installed("boto3"):
            from opentelemetry.instrumentation.bedrock import BedrockInstrumentor

            instrumentor = BedrockInstrumentor(
                exception_logger=lambda e: Telemetry().log_exception(e),
                enrich_token_usage=should_enrich_metrics,
            )
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        return True
    except Exception as e:
        logging.error(f"Error initializing Bedrock instrumentor: {e}")
        Telemetry().log_exception(e)
        return False


def init_sagemaker_instrumentor(should_enrich_metrics: bool):
    try:
        if is_package_installed("boto3"):
            from opentelemetry.instrumentation.sagemaker import SageMakerInstrumentor

            instrumentor = SageMakerInstrumentor(
                exception_logger=lambda e: Telemetry().log_exception(e),
                enrich_token_usage=should_enrich_metrics,
            )
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        return True
    except Exception as e:
        logging.error(f"Error initializing SageMaker instrumentor: {e}")
        Telemetry().log_exception(e)
        return False


def init_vertexai_instrumentor():
    try:
        if is_package_installed("google-cloud-aiplatform"):
            Telemetry().capture("instrumentation:vertexai:init")
            from opentelemetry.instrumentation.vertexai import VertexAIInstrumentor

            instrumentor = VertexAIInstrumentor(
                exception_logger=lambda e: Telemetry().log_exception(e),
            )
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        return True
    except Exception as e:
        logging.warning(f"Error initializing Vertex AI instrumentor: {e}")
        Telemetry().log_exception(e)
        return False


def init_groq_instrumentor():
    try:
        if is_package_installed("groq"):
            Telemetry().capture("instrumentation:groq:init")
            from opentelemetry.instrumentation.groq import GroqInstrumentor

            instrumentor = GroqInstrumentor(
                exception_logger=lambda e: Telemetry().log_exception(e),
            )
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        return True
    except Exception as e:
        logging.error(f"Error initializing Groq instrumentor: {e}")
        Telemetry().log_exception(e)
        return False


def init_crewai_instrumentor():
    try:
        if is_package_installed("crewai"):
            Telemetry().capture("instrumentation:crewai:init")
            from opentelemetry.instrumentation.crewai import CrewAIInstrumentor

            instrumentor = CrewAIInstrumentor(
                exception_logger=lambda e: Telemetry().log_exception(e),
            )
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        return True
    except Exception as e:
        logging.error(f"Error initializing CrewAI instrumentor: {e}")
        Telemetry().log_exception(e)
        return False


def metrics_common_attributes():
    common_attributes = {}
    workflow_name = get_value("workflow_name")
    if workflow_name is not None:
        common_attributes[OBSERVE_WORKFLOW_NAME] = workflow_name

    entity_name = get_value("entity_name")
    if entity_name is not None:
        common_attributes[OBSERVE_ENTITY_NAME] = entity_name

    association_properties = get_value("association_properties")
    if association_properties is not None:
        for key, value in association_properties.items():
            common_attributes[f"{OBSERVE_ASSOCIATION_PROPERTIES}.{key}"] = value

    return common_attributes
