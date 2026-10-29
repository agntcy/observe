# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import pytest
from opentelemetry._logs import get_logger_provider
from opentelemetry.sdk._logs.export import (
    LogRecordExporter,
    LogRecordExportResult,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind

from ioa_observe.sdk.tracing import (
    RuntimeEventAttribute,
    RuntimeEventName,
    RuntimeEventSpanProcessor,
)
from ioa_observe.sdk.tracing.runtime_events import identify_runtime_event
from ioa_observe.sdk.utils.const import OBSERVE_AGENT_SPAN_ID, OBSERVE_AGENT_TRACE_ID


class MemoryLogExporter(LogRecordExporter):
    def __init__(self):
        self.records = []

    def export(self, batch):
        self.records.extend(batch)
        return LogRecordExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def test_processor_exports_correlated_event_logs_without_replacing_global_provider():
    global_logger_provider = get_logger_provider()
    resource = Resource.create({"service.name": "third-party-agent"})
    exporter = MemoryLogExporter()
    provider = TracerProvider()
    processor = RuntimeEventSpanProcessor(
        exporter=exporter,
        resource=resource,
        schedule_delay_millis=10,
        derive_events=False,
    )
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("third.party.genai", "1.2.3")

    with tracer.start_as_current_span(
        "invoke_agent Weather Agent",
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.conversation.id": "conversation-123",
            "gen_ai.agent.name": "Weather Agent",
            OBSERVE_AGENT_SPAN_ID: "0000000000000123",
            OBSERVE_AGENT_TRACE_ID: "00000000000000000000000000000456",
        },
    ) as span:
        span_context = span.get_span_context()

    assert processor.force_flush(timeout_millis=1_000)
    assert get_logger_provider() is global_logger_provider
    assert [record.log_record.event_name for record in exporter.records] == [
        RuntimeEventName.TOPOLOGY_NODE_STARTED.value,
        RuntimeEventName.TOPOLOGY_NODE_COMPLETED.value,
    ]
    assert [
        record.log_record.attributes[RuntimeEventAttribute.SNAPSHOT_VERSION.value]
        for record in exporter.records
    ] == [1, 2]
    assert all(
        record.log_record.trace_id == span_context.trace_id
        and record.log_record.span_id == span_context.span_id
        for record in exporter.records
    )
    assert all(
        record.log_record.attributes[OBSERVE_AGENT_SPAN_ID] == "0000000000000123"
        and record.log_record.attributes[OBSERVE_AGENT_TRACE_ID]
        == "00000000000000000000000000000456"
        for record in exporter.records
    )
    assert all(
        record.instrumentation_scope.name == "third.party.genai"
        and record.instrumentation_scope.version == "1.2.3"
        for record in exporter.records
    )
    assert all(
        record.resource.attributes["service.name"] == "third-party-agent"
        for record in exporter.records
    )
    for record in exporter.records:
        attributes = record.log_record.attributes
        assert attributes["event.id"] == identify_runtime_event(attributes)["event.id"]
    assert (
        len({record.log_record.attributes["event.id"] for record in exporter.records})
        == 2
    )

    provider.shutdown()


@pytest.mark.parametrize(
    ("operation", "kind", "extra_attributes", "input_key", "output_key"),
    [
        (
            "invoke_agent",
            SpanKind.INTERNAL,
            {},
            "agent.input",
            "agent.output",
        ),
        (
            "chat",
            SpanKind.CLIENT,
            {"gen_ai.provider.name": "openai", "gen_ai.request.model": "gpt-5"},
            "llm.input",
            "llm.output",
        ),
        (
            "execute_tool",
            SpanKind.INTERNAL,
            {"gen_ai.tool.name": "get_weather"},
            "tool.input",
            "tool.output",
        ),
    ],
)
def test_processor_exports_agent_attribution_and_content(
    operation, kind, extra_attributes, input_key, output_key
):
    exporter = MemoryLogExporter()
    provider = TracerProvider()
    processor = RuntimeEventSpanProcessor(exporter=exporter, derive_events=False)
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("third.party.genai")
    is_tool = operation == "execute_tool"
    input_attribute = (
        "gen_ai.tool.call.arguments" if is_tool else "gen_ai.input.messages"
    )
    output_attribute = (
        "gen_ai.tool.call.result" if is_tool else "gen_ai.output.messages"
    )
    input_content = '{"city":"Paris"}' if is_tool else '[{"role":"user","parts":[]}]'
    output_content = (
        '{"temperature":22}' if is_tool else '[{"role":"assistant","parts":[]}]'
    )

    try:
        with tracer.start_as_current_span(
            operation,
            kind=kind,
            attributes={
                "gen_ai.operation.name": operation,
                "gen_ai.conversation.id": "conversation-123",
                "gen_ai.agent.name": "Weather Agent",
                input_attribute: input_content,
                **extra_attributes,
            },
        ) as span:
            span.set_attribute(output_attribute, output_content)

        assert processor.force_flush(timeout_millis=1_000)
        assert len(exporter.records) == 2
        started, completed = [
            record.log_record.attributes for record in exporter.records
        ]
        for attributes in (started, completed):
            assert attributes["agent.name"] == "Weather Agent"
            assert attributes[input_key] == input_content
            assert (
                attributes["event.id"] == identify_runtime_event(attributes)["event.id"]
            )
        assert output_key not in started
        assert completed[output_key] == output_content
    finally:
        provider.shutdown()
