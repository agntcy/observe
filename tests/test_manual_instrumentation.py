# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import json
from typing import (
    Any,
    Dict,
)
import pytest
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.semconv_ai import SpanAttributes
from opentelemetry.trace import Status, StatusCode, Tracer, TracerProvider

from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import agent, workflow, tool
from ioa_observe.sdk.tracing.manual import track_llm_call, LLMMessage
from ioa_observe.sdk.utils.const import (
    ObserveSpanKindValues,
    OBSERVE_ENTITY_INPUT,
    OBSERVE_ENTITY_OUTPUT,
    OBSERVE_SPAN_KIND,
    OBSERVE_ASSOCIATION_PROPERTIES,
)


def remove_all_vcr_request_headers(request: Any) -> Any:
    """
    Removes all request headers.

    Example:
    ```
    @pytest.mark.vcr(
        before_record_response=remove_all_vcr_request_headers
    )
    def test_openai() -> None:
        # make request to OpenAI
    """
    request.headers.clear()
    return request


def remove_all_vcr_response_headers(response: Dict[str, Any]) -> Dict[str, Any]:
    """
    Removes all response headers.

    Example:
    ```
    @pytest.mark.vcr(
        before_record_response=remove_all_vcr_response_headers
    )
    def test_openai() -> None:
        # make request to OpenAI
    """
    response["headers"] = {}
    return response


@pytest.fixture
def tracer(tracer_provider: TracerProvider) -> Tracer:
    return tracer_provider.get_tracer(__name__)


class TestStartAsCurrentSpanContextManager:
    def test_chain_with_plain_text_input_and_output(
        self,
        in_memory_span_exporter: InMemorySpanExporter,
        tracer: Tracer,
    ) -> None:
        with tracer.start_as_current_span(
            "span-name",
            kind=ObserveSpanKindValues.AGENT,
        ) as chain_span:
            chain_span.set_attribute(OBSERVE_ENTITY_INPUT, "plain-text-input")
            chain_span.set_attribute(OBSERVE_ENTITY_OUTPUT, "plain-text-output")
            chain_span.set_attribute("status", Status(StatusCode.OK))
            chain_span.set_attribute(
                OBSERVE_SPAN_KIND, ObserveSpanKindValues.AGENT.value
            )

        spans = in_memory_span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "span-name"
        assert span.status.is_ok
        assert not span.events
        attributes = dict(span.attributes or {})
        assert attributes.pop(OBSERVE_SPAN_KIND) == ObserveSpanKindValues.AGENT.value
        assert attributes.pop(OBSERVE_ENTITY_INPUT) == "plain-text-input"
        assert attributes.pop(OBSERVE_ENTITY_OUTPUT) == "plain-text-output"
        assert attributes.pop("ioa_start_time") is not None
        assert not attributes

    def test_chain_with_json_input_and_output(
        self,
        in_memory_span_exporter: InMemorySpanExporter,
        tracer: Tracer,
    ) -> None:
        with tracer.start_as_current_span(
            "span-name",
            kind=ObserveSpanKindValues.AGENT,
        ) as chain_span:
            chain_span.set_attribute(
                OBSERVE_ENTITY_INPUT,
                json.dumps({"input-key": "input-value"}),
            )
            chain_span.set_attribute(
                OBSERVE_SPAN_KIND, ObserveSpanKindValues.AGENT.value
            )
            chain_span.set_attribute(
                OBSERVE_ENTITY_OUTPUT,
                json.dumps({"output-key": "output-value"}),
            )
            chain_span.set_attribute("status", Status(StatusCode.OK))

        spans = in_memory_span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "span-name"
        assert span.status.is_ok
        assert not span.events
        attributes = dict(span.attributes or {})
        assert attributes.pop(OBSERVE_SPAN_KIND) == ObserveSpanKindValues.AGENT.value
        assert attributes.pop(OBSERVE_ENTITY_INPUT) == json.dumps(
            {"input-key": "input-value"}
        )
        assert attributes.pop(OBSERVE_ENTITY_OUTPUT) == json.dumps(
            {"output-key": "output-value"}
        )
        assert attributes.pop("ioa_start_time") is not None
        assert not attributes

    def test_agent(
        self,
        in_memory_span_exporter: InMemorySpanExporter,
        tracer: Tracer,
    ) -> None:
        with tracer.start_as_current_span(
            "agent-span-name",
            kind=ObserveSpanKindValues.AGENT,
        ) as agent_span:
            agent_span.set_attribute(OBSERVE_ENTITY_INPUT, "plain-text-input")
            agent_span.set_attribute(OBSERVE_ENTITY_OUTPUT, "plain-text-output")
            agent_span.set_attribute("status", Status(StatusCode.OK))
            agent_span.set_attribute(
                OBSERVE_SPAN_KIND, ObserveSpanKindValues.AGENT.value
            )

        spans = in_memory_span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "agent-span-name"
        assert span.status.is_ok
        assert not span.events
        attributes = dict(span.attributes or {})
        assert attributes.pop(OBSERVE_SPAN_KIND) == ObserveSpanKindValues.AGENT.value
        assert attributes.pop(OBSERVE_ENTITY_INPUT) == "plain-text-input"
        assert attributes.pop(OBSERVE_ENTITY_OUTPUT) == "plain-text-output"
        assert attributes.pop("ioa_start_time") is not None
        assert not attributes

    def test_custom_attribute_span(
        self,
        in_memory_span_exporter: InMemorySpanExporter,
        tracer: Tracer,
    ) -> None:
        with tracer.start_as_current_span(
            "non-openinference-span"
        ) as non_openinference_span:
            non_openinference_span.set_attribute("custom.attribute", "value")

        spans = in_memory_span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "non-openinference-span"
        attributes = dict(span.attributes or {})
        assert attributes.pop("custom.attribute") == "value"
        assert attributes.pop("ioa_start_time") is not None
        assert not attributes

    def test_tool(
        self,
        in_memory_span_exporter: InMemorySpanExporter,
        tracer: Tracer,
    ) -> None:
        with tracer.start_as_current_span(
            "tool-span-name",
            kind=ObserveSpanKindValues.TOOL,
        ) as tool_span:
            tool_span.set_attribute(OBSERVE_ENTITY_INPUT, "plain-text-input")
            tool_span.set_attribute(OBSERVE_ENTITY_OUTPUT, "plain-text-output")
            tool_span.set_attribute("status", Status(StatusCode.OK))
            tool_span.set_attribute(OBSERVE_SPAN_KIND, ObserveSpanKindValues.TOOL.value)

        spans = in_memory_span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "tool-span-name"
        assert span.status.is_ok
        assert not span.events
        attributes = dict(span.attributes or {})
        assert attributes.pop(OBSERVE_SPAN_KIND) == ObserveSpanKindValues.TOOL.value
        assert attributes.pop(OBSERVE_ENTITY_INPUT) == "plain-text-input"
        assert attributes.pop(OBSERVE_ENTITY_OUTPUT) == "plain-text-output"
        # assert attributes.pop(TOOL_NAME) == "tool-name"
        # assert attributes.pop(TOOL_DESCRIPTION) == "tool-description"
        # assert isinstance(tool_parameters := attributes.pop(TOOL_PARAMETERS), str)
        # assert json.loads(tool_parameters) == {"type": "string"}
        assert attributes.pop("ioa_start_time") is not None
        assert not attributes


@pytest.mark.vcr
def test_manual_report(exporter_with_custom_span_processor, openai_client):
    with track_llm_call(vendor="openai", type="chat") as span:
        span.report_request(
            model="gpt-3.5-turbo",
            messages=[
                LLMMessage(role="user", content="Tell me a joke about opentelemetry")
            ],
        )

        res = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "user", "content": "Tell me a joke about opentelemetry"}
            ],
        )
        span.report_response(res.model, [text.message.content for text in res.choices])

    import time

    time.sleep(0.5)  # Increase delay to ensure span is exported
    spans = exporter_with_custom_span_processor.get_finished_spans()

    # Only continue if we have spans
    if not spans:
        pytest.fail(
            "No spans were recorded. Tracing might not be properly initialized."
        )

    open_ai_span = spans[0]
    assert open_ai_span.attributes[SpanAttributes.LLM_REQUEST_MODEL] == "gpt-3.5-turbo"
    assert open_ai_span.attributes[f"{SpanAttributes.LLM_PROMPTS}.0.role"] == "user"
    assert (
        open_ai_span.attributes[f"{SpanAttributes.LLM_PROMPTS}.0.content"]
        == "Tell me a joke about opentelemetry"
    )
    assert (
        open_ai_span.attributes[SpanAttributes.LLM_RESPONSE_MODEL]
        == "gpt-3.5-turbo-0125"
    )
    assert (
        open_ai_span.attributes[f"{SpanAttributes.LLM_COMPLETIONS}.0.content"]
        is not None
    )
    # assert if tokens are present
    assert open_ai_span.attributes[SpanAttributes.LLM_USAGE_TOTAL_TOKENS] is not None

    assert (
        open_ai_span.attributes[SpanAttributes.LLM_USAGE_COMPLETION_TOKENS] is not None
    )
    assert open_ai_span.end_time > open_ai_span.start_time


def test_association_properties(exporter_with_custom_span_processor):
    @workflow(name="test_workflow")
    def test_workflow():
        return test_tool()

    @tool(name="test_tool")
    def test_tool():
        return

    Observe.set_association_properties({"user_id": 1, "user_name": "John Doe"})
    test_workflow()

    spans = exporter_with_custom_span_processor.get_finished_spans()
    assert [span.name for span in spans] == [
        "test_tool.tool",
        "test_workflow.workflow",
    ]

    some_task_span = spans[0]
    some_workflow_span = spans[1]
    assert (
        some_workflow_span.attributes[f"{OBSERVE_ASSOCIATION_PROPERTIES}.user_id"] == 1
    )
    assert (
        some_workflow_span.attributes[f"{OBSERVE_ASSOCIATION_PROPERTIES}.user_name"]
        == "John Doe"
    )
    assert some_task_span.attributes[f"{OBSERVE_ASSOCIATION_PROPERTIES}.user_id"] == 1
    assert (
        some_task_span.attributes[f"{OBSERVE_ASSOCIATION_PROPERTIES}.user_name"]
        == "John Doe"
    )


def test_association_properties_within_workflow(exporter_with_custom_span_processor):
    @agent(name="test_agent_within")
    def test_agent():
        Observe.set_association_properties({"session_id": 15})
        return

    test_agent()

    spans = exporter_with_custom_span_processor.get_finished_spans()
    assert set(span.name for span in spans) == {
        "agent_start_event",
        "test_agent_within.agent",
        "agent_end_event",
    }

    some_workflow_span = spans[1]
    assert (
        some_workflow_span.attributes[f"{OBSERVE_ASSOCIATION_PROPERTIES}.session_id"]
        == 15
    )


@pytest.mark.vcr
def test_langchain_association_properties(exporter_with_custom_span_processor):
    prompt = ChatPromptTemplate.from_messages(
        [("system", "You are helpful assistant"), ("user", "{input}")]
    )
    model = ChatOpenAI(model="gpt-3.5-turbo")

    chain = prompt | model
    chain.invoke(
        {"input": "tell me a short joke"},
        {"metadata": {"user_id": "1234", "session_id": 456}},
    )

    spans = exporter_with_custom_span_processor.get_finished_spans()

    assert [
        "ChatPromptTemplate.task",
        "ChatOpenAI.chat",
        "RunnableSequence.workflow",
    ] == [span.name for span in spans]

    workflow_span = next(
        span for span in spans if span.name == "RunnableSequence.workflow"
    )
    prompt_span = next(span for span in spans if span.name == "ChatPromptTemplate.task")
    chat_span = next(span for span in spans if span.name == "ChatOpenAI.chat")

    assert (
        workflow_span.attributes[f"{OBSERVE_ASSOCIATION_PROPERTIES}.user_id"] == "1234"
    )
    assert (
        workflow_span.attributes[f"{OBSERVE_ASSOCIATION_PROPERTIES}.session_id"] == 456
    )
    assert chat_span.attributes[f"{OBSERVE_ASSOCIATION_PROPERTIES}.user_id"] == "1234"
    assert chat_span.attributes[f"{OBSERVE_ASSOCIATION_PROPERTIES}.session_id"] == 456
    assert prompt_span.attributes[f"{OBSERVE_ASSOCIATION_PROPERTIES}.user_id"] == "1234"
    assert prompt_span.attributes[f"{OBSERVE_ASSOCIATION_PROPERTIES}.session_id"] == 456
