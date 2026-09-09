# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from opentelemetry._logs import get_logger_provider
from opentelemetry.sdk._logs.export import (
    LogRecordExporter,
    LogRecordExportResult,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from ioa_observe.sdk.tracing import (
    RuntimeEventAttribute,
    RuntimeEventName,
    RuntimeEventSpanProcessor,
)


class MemoryLogExporter(LogRecordExporter):
    def __init__(self):
        self.records = []

    def export(self, batch):
        self.records.extend(batch)
        return LogRecordExportResult.SUCCESS

    def shutdown(self):
        pass


def test_processor_exports_correlated_event_logs_without_replacing_global_provider():
    global_logger_provider = get_logger_provider()
    resource = Resource.create({"service.name": "third-party-agent"})
    exporter = MemoryLogExporter()
    provider = TracerProvider()
    processor = RuntimeEventSpanProcessor(
        exporter=exporter,
        resource=resource,
        schedule_delay_millis=10,
    )
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("third.party.genai", "1.2.3")

    with tracer.start_as_current_span(
        "invoke_agent Weather Agent",
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.conversation.id": "conversation-123",
            "gen_ai.agent.name": "Weather Agent",
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
        record.instrumentation_scope.name == "third.party.genai"
        and record.instrumentation_scope.version == "1.2.3"
        for record in exporter.records
    )
    assert all(
        record.resource.attributes["service.name"] == "third-party-agent"
        for record in exporter.records
    )

    provider.shutdown()
