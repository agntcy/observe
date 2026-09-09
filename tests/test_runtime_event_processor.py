# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from datetime import datetime, timezone

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind

from ioa_observe.sdk.tracing.runtime_event_processor import (
    GenAIRuntimeEventMapper,
    InstrumentationScope,
    SpanLifecycle,
    SpanLifecycleObservation,
)
from ioa_observe.sdk.tracing.runtime_events import (
    RuntimeEventAttribute,
    RuntimeEventName,
)


def observation(
    lifecycle: SpanLifecycle,
    attributes: dict,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    span_id: int = 2,
) -> SpanLifecycleObservation:
    return SpanLifecycleObservation(
        lifecycle=lifecycle,
        name="third-party GenAI operation",
        kind=kind,
        attributes=attributes,
        trace_id=1,
        span_id=span_id,
        instrumentation_scope=InstrumentationScope("third.party.genai", "1.2.3"),
        resource_attributes={"service.name": "third-party-agent"},
        observed_time=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )


def test_observation_captures_public_otel_correlation_as_immutable_snapshot():
    provider = TracerProvider(
        resource=Resource.create({"service.name": "third-party-agent"})
    )
    tracer = provider.get_tracer("third.party.genai", "1.2.3")
    span = tracer.start_span(
        "invoke_agent Weather Agent",
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.conversation.id": "conversation-123",
        },
    )

    captured = SpanLifecycleObservation.from_span(SpanLifecycle.START, span)
    span.set_attribute("gen_ai.conversation.id", "changed")

    assert captured.trace_id == span.get_span_context().trace_id
    assert captured.span_id == span.get_span_context().span_id
    assert captured.instrumentation_scope.name == "third.party.genai"
    assert captured.instrumentation_scope.version == "1.2.3"
    assert captured.resource_attributes["service.name"] == "third-party-agent"
    assert captured.attributes["gen_ai.conversation.id"] == "conversation-123"
    with pytest.raises(TypeError):
        captured.attributes["new"] = "value"

    span.end()


def test_genai_agent_invocation_maps_to_topology_node_lifecycle():
    mapper = GenAIRuntimeEventMapper()
    attributes = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.conversation.id": "conversation-123",
        "gen_ai.agent.name": "Weather Agent",
    }

    started = mapper(observation(SpanLifecycle.START, attributes), 1)[0]
    completed = mapper(observation(SpanLifecycle.END, attributes), 2)[0]

    assert started.name == RuntimeEventName.TOPOLOGY_NODE_STARTED
    assert completed.name == RuntimeEventName.TOPOLOGY_NODE_COMPLETED
    assert started.session_id == "conversation-123"
    assert started.attributes == {
        RuntimeEventAttribute.AGENT_NAME.value: "Weather Agent",
        "gen_ai.operation.name": "invoke_agent",
    }
    assert (
        started.to_otel_attributes()[RuntimeEventAttribute.SNAPSHOT_VERSION.value] == 1
    )


def test_genai_tool_execution_maps_standardized_input_and_output():
    mapper = GenAIRuntimeEventMapper()
    attributes = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.conversation.id": "conversation-123",
        "gen_ai.tool.name": "get_weather",
        "gen_ai.tool.call.id": "call-123",
        "gen_ai.tool.call.arguments": '{"city":"Paris"}',
    }

    started = mapper(observation(SpanLifecycle.START, attributes), 1)[0]
    completed = mapper(
        observation(
            SpanLifecycle.END,
            {**attributes, "gen_ai.tool.call.result": '{"temperature":22}'},
        ),
        2,
    )[0]

    assert started.name == RuntimeEventName.TOOL_STARTED
    assert started.attributes[RuntimeEventAttribute.TOOL_NAME.value] == "get_weather"
    assert (
        started.attributes[RuntimeEventAttribute.TOOL_INPUT.value] == '{"city":"Paris"}'
    )
    assert completed.name == RuntimeEventName.TOOL_COMPLETED
    assert (
        completed.attributes[RuntimeEventAttribute.TOOL_OUTPUT.value]
        == '{"temperature":22}'
    )


def test_genai_model_inference_maps_standardized_model_and_messages():
    mapper = GenAIRuntimeEventMapper()
    attributes = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "openai",
        "gen_ai.conversation.id": "conversation-123",
        "gen_ai.request.model": "gpt-5",
        "gen_ai.input.messages": '[{"role":"user","parts":[]}]',
    }

    started = mapper(
        observation(
            SpanLifecycle.START,
            attributes,
            kind=SpanKind.CLIENT,
            span_id=0xABC,
        ),
        1,
    )[0]
    completed = mapper(
        observation(
            SpanLifecycle.END,
            {
                **attributes,
                "gen_ai.response.model": "gpt-5-2026-08-01",
                "gen_ai.response.id": "response-123",
                "gen_ai.output.messages": '[{"role":"assistant","parts":[]}]',
            },
            kind=SpanKind.CLIENT,
            span_id=0xABC,
        ),
        2,
    )[0]

    assert started.name == RuntimeEventName.LLM_STARTED
    assert started.attributes[RuntimeEventAttribute.LLM_NAME.value] == "gpt-5"
    assert (
        started.attributes[RuntimeEventAttribute.LLM_INPUT.value]
        == '[{"role":"user","parts":[]}]'
    )
    assert started.attributes[RuntimeEventAttribute.LLM_CALL_ID.value] == (
        "0000000000000abc"
    )
    assert completed.name == RuntimeEventName.LLM_COMPLETED
    assert completed.attributes[RuntimeEventAttribute.LLM_NAME.value] == (
        "gpt-5-2026-08-01"
    )
    assert completed.attributes[RuntimeEventAttribute.LLM_OUTPUT.value] == (
        '[{"role":"assistant","parts":[]}]'
    )


def test_late_genai_attributes_can_map_completion_but_not_start():
    mapper = GenAIRuntimeEventMapper()
    start = observation(
        SpanLifecycle.START,
        {"gen_ai.conversation.id": "conversation-123"},
    )
    end = observation(
        SpanLifecycle.END,
        {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.conversation.id": "conversation-123",
            "gen_ai.tool.name": "get_weather",
        },
    )

    assert mapper(start, 1) == ()
    assert mapper(end, 2)[0].name == RuntimeEventName.TOOL_COMPLETED


@pytest.mark.parametrize(
    ("attributes", "kind"),
    [
        (
            {
                "llm.request.type": "chat",
                "session.id": "legacy-session",
                "gen_ai.request.model": "gpt-5",
            },
            SpanKind.CLIENT,
        ),
        (
            {
                "gen_ai.operation.name": "embeddings",
                "gen_ai.provider.name": "openai",
                "gen_ai.conversation.id": "conversation-123",
                "gen_ai.request.model": "text-embedding-3-small",
            },
            SpanKind.CLIENT,
        ),
        (
            {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.conversation.id": "conversation-123",
                "gen_ai.tool.name": "get_weather",
            },
            SpanKind.CLIENT,
        ),
        (
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.conversation.id": "conversation-123",
                "gen_ai.request.model": "gpt-5",
            },
            SpanKind.CLIENT,
        ),
        (
            {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": "Weather Agent",
            },
            SpanKind.INTERNAL,
        ),
        (
            {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.conversation.id": "conversation-123",
                "gen_ai.agent.name": "Weather Agent",
            },
            SpanKind.CLIENT,
        ),
    ],
)
def test_noncompliant_or_unsupported_spans_emit_nothing(attributes, kind):
    mapper = GenAIRuntimeEventMapper()

    assert mapper(observation(SpanLifecycle.START, attributes, kind=kind), 1) == ()


def test_mapper_rejects_non_positive_snapshot_version():
    with pytest.raises(ValueError, match="snapshot_version must be positive"):
        GenAIRuntimeEventMapper()(
            observation(
                SpanLifecycle.START,
                {
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.conversation.id": "conversation-123",
                    "gen_ai.agent.name": "Weather Agent",
                },
            ),
            0,
        )
