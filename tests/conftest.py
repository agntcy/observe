import os
import re

import pytest
from openai import OpenAI
from opentelemetry.context import attach, Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, BatchSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ioa_observe.sdk import Observe
from ioa_observe.sdk.tracing.tracing import init_tracer_provider, TracerWrapper


@pytest.fixture()
def in_memory_span_exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def openai_client():
    return OpenAI()


@pytest.fixture(scope="module")
def vcr_config():
    return {
        "filter_headers": ["authorization"],
        "ignore_hosts": ["openaipublic.blob.core.windows.net"],
    }


@pytest.fixture()
def tracer_provider(in_memory_span_exporter: InMemorySpanExporter) -> TracerProvider:
    resource = Resource(attributes=TracerWrapper.resource_attributes)
    tracer_provider = init_tracer_provider(resource=resource)
    tracer_provider.add_span_processor(SimpleSpanProcessor(in_memory_span_exporter))
    return tracer_provider


@pytest.fixture(scope="session")
def exporter():
    exporter = InMemorySpanExporter()
    Observe.init(
        app_name="test",
        exporter=exporter,
    )
    return exporter


@pytest.fixture(autouse=True)
def clear_exporter(exporter):
    exporter.clear()
    # Reset the tracing context to ensure tests don't affect each other
    # Create a new empty context and attach it
    # This effectively removes all previous context values
    attach(Context())


@pytest.fixture(autouse=True)
def environment():
    if "OPENAI_API_KEY" not in os.environ:
        os.environ["OPENAI_API_KEY"] = "test_api_key"


@pytest.fixture
def exporter_with_custom_span_processor():
    # Clear singleton if existed
    if hasattr(TracerWrapper, "instance"):
        _trace_wrapper_instance = TracerWrapper.instance
        del TracerWrapper.instance

    class CustomSpanProcessor(SimpleSpanProcessor):
        def on_start(self, span, parent_context=None):
            span.set_attribute("custom_span", "yes")

    exporter = InMemorySpanExporter()
    Observe.init(
        exporter=exporter,
        processor=CustomSpanProcessor(exporter),
    )

    yield exporter

    # Restore singleton if any
    if _trace_wrapper_instance:
        TracerWrapper.instance = _trace_wrapper_instance


@pytest.fixture(scope="function")
def exporter_with_custom_span_postprocess_callback(exporter):
    if hasattr(TracerWrapper, "instance"):
        _trace_wrapper_instance = TracerWrapper.instance
        del TracerWrapper.instance

    def span_postprocess_callback(span: ReadableSpan) -> None:
        prompt_pattern = re.compile(r"gen_ai\.prompt\.\d+\.content$")
        completion_pattern = re.compile(r"gen_ai\.completion\.\d+\.content$")
        if hasattr(span, "_attributes"):
            attributes = span._attributes if span._attributes else {}
            # Find and encode all matching attributes
            for key, value in attributes.items():
                if (
                    prompt_pattern.match(key) or completion_pattern.match(key)
                ) and isinstance(value, str):
                    attributes[key] = "REDACTED"  # Modify the attributes directly

    Observe.init(
        exporter=exporter,
        span_postprocess_callback=span_postprocess_callback,
    )

    yield exporter

    if hasattr(TracerWrapper, "instance"):
        # Get the span processor
        if hasattr(TracerWrapper.instance, "_TracerWrapper__spans_processor"):
            span_processor = TracerWrapper.instance._TracerWrapper__spans_processor
            # Reset the on_end method to its original class implementation.
            # This is needed to make this test run in isolation as SpanProcessor is a singleton.
            if isinstance(span_processor, SimpleSpanProcessor):
                span_processor.on_end = SimpleSpanProcessor.on_end.__get__(
                    span_processor, SimpleSpanProcessor
                )
            elif isinstance(span_processor, BatchSpanProcessor):
                span_processor.on_end = BatchSpanProcessor.on_end.__get__(
                    span_processor, BatchSpanProcessor
                )
    if _trace_wrapper_instance:
        TracerWrapper.instance = _trace_wrapper_instance


@pytest.fixture
def exporter_with_no_metrics():
    # Clear singleton if existed
    if hasattr(TracerWrapper, "instance"):
        _trace_wrapper_instance = TracerWrapper.instance
        del TracerWrapper.instance

    os.environ["OBSERVE_METRICS_ENABLED"] = "false"

    exporter = InMemorySpanExporter()

    Observe.init(
        exporter=exporter,
        disable_batch=True,
    )

    yield exporter

    # Restore singleton if any
    if _trace_wrapper_instance:
        TracerWrapper.instance = _trace_wrapper_instance
        os.environ["OBSERVE_METRICS_ENABLED"] = "true"
