# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import atexit
import contextlib
import json
import logging
import os
import re
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
from opentelemetry.trace import get_tracer_provider, ProxyTracerProvider
from opentelemetry.context import get_value, attach, set_value
from opentelemetry.instrumentation.threading import ThreadingInstrumentor
from opentelemetry.metrics import get_meter
from ioa_observe.sdk.metrics.agents.agent_connections import connection_reliability
from opentelemetry.semconv_ai import SpanAttributes
from ioa_observe.sdk import Telemetry
from ioa_observe.sdk.instruments import Instruments
from ioa_observe.sdk.tracing.content_allow_list import ContentAllowList
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
    endpoint: str = (None,)
    app_name: str = (None,)
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

            obj.__image_uploader = image_uploader
            obj._agent_execution_counts = {}  # {(agent_name): [success_count, total_count]}
            TracerWrapper.app_name = TracerWrapper.resource_attributes.get(
                "service.name", "observe"
            )
            obj.__resource = Resource(attributes=TracerWrapper.resource_attributes)
            obj.__tracer_provider = init_tracer_provider(resource=obj.__resource)
            if processor:
                Telemetry().capture("tracer:init", {"processor": "custom"})
                obj.__spans_processor: SpanProcessor = processor
                obj.__spans_processor_original_on_start = processor.on_start
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

            obj.__spans_processor.on_start = obj._span_processor_on_start
            # obj.__spans_processor.on_end = obj.span_processor_on_ending
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
            obj.response_latency_histogram = meter.create_histogram(
                name="response_latency",
                description="Records the latency of responses",
                unit="ms",
            )
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
            obj.processing_time = meter.create_histogram(
                "slim.message.processing_time",
                description="Time taken to process SLIM messages",
            )
            obj.throughput_counter = meter.create_counter(
                "slim.message.throughput",
                description="Message throughput for SLIM operations",
            )
            obj.error_counter = meter.create_counter(
                "slim.errors", description="Number of SLIM message errors or drops"
            )
            obj.agent_chain_completion_time_histogram = meter.create_histogram(
                name="gen_ai.client.ioa.agent.end_to_end_chain_completion_time",
                description="Records the end-to-end chain completion time for a single agent",
                unit="s",
            )
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

            # Force flushes for debug environments (e.g. local development)
            atexit.register(obj.exit_handler)

        return cls.instance

    def exit_handler(self):
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
        determine_reliability_score(span)
        start_time = span.attributes.get("ioa_start_time")
        # publish span to the exporter

        if start_time is not None:
            latency = (time.time() - start_time) * 1000
            self.response_latency_histogram.record(latency, attributes=span.attributes)

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
        counts = self._agent_execution_counts.setdefault(agent_name, [0, 0])
        if success:
            counts[0] += 1  # success count
        counts[1] += 1  # total count

    def _observe_agent_execution_success_rate(self, observer):
        measurements = []
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


def session_start():
    """
    Can be used as a context manager or a normal function.
    As a context manager, yields session metadata.
    As a normal function, just sets up the session.
    """
    session_id = TracerWrapper.app_name + "_" + str(uuid.uuid4())
    set_session_id(session_id)
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

    if current_span.is_recording():
        # We have an active span, use its context
        carrier = {}
        TraceContextTextMapPropagator().inject(carrier)
        extracted_traceparent = carrier.get("traceparent")
    else:
        # Only create new span if absolutely necessary (no existing context)
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("set_session_id"):
            carrier = {}
            TraceContextTextMapPropagator().inject(carrier)
            extracted_traceparent = carrier.get("traceparent")

    # Store execution ID with traceparent as key
    if extracted_traceparent:
        kv_key = f"execution.{extracted_traceparent}"
        if kv_store.get(kv_key) is None:
            kv_store.set(kv_key, session_id)

        # Also store in OpenTelemetry context
        attach(set_value("session.id", session_id))
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
    if "http" in api_endpoint.lower() or "https" in api_endpoint.lower():
        return HTTPExporter(
            endpoint=f"{api_endpoint}/v1/traces",
            headers=headers,
            compression=Compression.Gzip,
        )
    else:
        return GRPCExporter(endpoint=f"{api_endpoint}", headers=headers)


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
