# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path

import pytest

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind

from ioa_observe.sdk.tracing import (
    RuntimeEventName,
    RuntimeEventSpanProcessor,
    normalize_span_attributes,
)


class ListEmitter:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    def force_flush(self, timeout_millis=30_000):
        return True

    def shutdown(self):
        pass


def _run(attributes, name="span", **processor_kwargs):
    emitter = ListEmitter()
    provider = TracerProvider()
    provider.add_span_processor(
        RuntimeEventSpanProcessor(
            emitter=emitter, **{"derive_events": False, **processor_kwargs}
        )
    )
    with provider.get_tracer("vendor.sdk").start_as_current_span(
        name, attributes=attributes
    ):
        pass
    return [event.name for event in emitter.events]


def test_existing_gen_ai_attributes_are_not_overwritten():
    result = normalize_span_attributes(
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.conversation.id": "keep",
            "session.id": "other",
            "openinference.span.kind": "TOOL",
        },
        "x",
    )
    assert result["gen_ai.operation.name"] == "chat"
    assert result["gen_ai.conversation.id"] == "keep"


def test_traceloop_tool_span_emits_tool_events():
    names = _run(
        {
            "traceloop.span.kind": "tool",
            "traceloop.entity.name": "search",
            "traceloop.entity.input": "{}",
            "traceloop.association.properties.session_id": "s1",
        }
    )
    assert names == [RuntimeEventName.TOOL_STARTED, RuntimeEventName.TOOL_COMPLETED]


def test_traceloop_llm_prompts_are_flattened():
    result = normalize_span_attributes(
        {
            "llm.request.type": "chat",
            "gen_ai.system": "OpenAI",
            "gen_ai.prompt.0.role": "user",
            "gen_ai.prompt.0.content": "hi",
            "gen_ai.completion.0.role": "assistant",
            "gen_ai.completion.0.content": "yo",
        },
        "openai.chat",
    )
    assert result["gen_ai.operation.name"] == "chat"
    assert result["gen_ai.provider.name"] == "OpenAI"
    assert json.loads(result["gen_ai.input.messages"]) == [
        {"role": "user", "content": "hi"}
    ]
    assert json.loads(result["gen_ai.output.messages"])[0]["content"] == "yo"


def test_openinference_agent_and_llm_spans():
    assert _run(
        {"openinference.span.kind": "AGENT", "session.id": "s1"}, name="planner"
    ) == [
        RuntimeEventName.TOPOLOGY_NODE_STARTED,
        RuntimeEventName.TOPOLOGY_NODE_COMPLETED,
    ]
    names = _run(
        {
            "openinference.span.kind": "LLM",
            "session.id": "s1",
            "llm.model_name": "gpt-4o",
            "llm.provider": "openai",
        }
    )
    assert names == [RuntimeEventName.LLM_STARTED, RuntimeEventName.LLM_COMPLETED]


def test_langfuse_generation_without_provider_uses_unknown():
    names = _run(
        {
            "langfuse.observation.type": "generation",
            "langfuse.session.id": "s1",
            "langfuse.observation.model.name": "claude",
        }
    )
    assert names == [RuntimeEventName.LLM_STARTED, RuntimeEventName.LLM_COMPLETED]


def test_langfuse_tool_span():
    names = _run(
        {"langfuse.observation.type": "tool", "langfuse.session.id": "s1"},
        name="lookup",
    )
    assert names == [RuntimeEventName.TOOL_STARTED, RuntimeEventName.TOOL_COMPLETED]


def test_session_fallback_and_missing_session_dropped():
    assert _run(
        {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "t",
            "thread_id": "s",
        }
    )
    assert not _run({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "t"})


def test_normalizers_can_be_disabled():
    assert not _run(
        {"openinference.span.kind": "TOOL", "session.id": "s1"}, normalizers=()
    )


FIXTURES = Path(__file__).parent / "fixtures" / "vendor_spans"


def _replay(vendor):
    """Replay spans recorded from a real vendor SDK, in completion order."""
    emitter = ListEmitter()
    provider = TracerProvider()
    provider.add_span_processor(
        RuntimeEventSpanProcessor(emitter=emitter, derive_events=False)
    )
    tracer = provider.get_tracer("replay")
    for recorded in json.loads((FIXTURES / f"{vendor}.json").read_text()):
        with tracer.start_as_current_span(
            recorded["name"],
            kind=SpanKind[recorded["kind"]],
            attributes=recorded["attributes"],
        ):
            pass
    return [
        (
            event.name,
            event.attributes.get("llm.name")
            or event.attributes.get("tool.name")
            or event.attributes.get("agent.name"),
        )
        for event in emitter.events
        if event.name.value.endswith(".completed")
    ]


@pytest.mark.parametrize(
    ("vendor", "expected"),
    [
        (
            "traceloop",
            [
                (RuntimeEventName.TOOL_COMPLETED, "get_weather"),
                (RuntimeEventName.LLM_COMPLETED, "gpt-4o-mini"),
                (RuntimeEventName.TOPOLOGY_NODE_COMPLETED, "weather_agent"),
            ],
        ),
        (
            "openinference",
            [
                (RuntimeEventName.TOOL_COMPLETED, "get_weather"),
                (RuntimeEventName.LLM_COMPLETED, "gpt-4o-mini"),
                (RuntimeEventName.TOPOLOGY_NODE_COMPLETED, "weather_agent"),
            ],
        ),
        (
            "langfuse",
            [
                (RuntimeEventName.TOOL_COMPLETED, "get_weather"),
                (RuntimeEventName.LLM_COMPLETED, "gpt-4o-mini"),
                (RuntimeEventName.TOPOLOGY_NODE_COMPLETED, "weather_agent"),
            ],
        ),
    ],
)
def test_recorded_vendor_spans_emit_runtime_events(vendor, expected):
    assert _replay(vendor) == expected
