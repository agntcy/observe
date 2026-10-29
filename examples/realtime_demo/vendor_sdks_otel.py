# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

"""Emit runtime events from Traceloop-, OpenInference- and Langfuse-style spans.

The spans below use each vendor's native attributes (no ``gen_ai.*`` agent/tool
keys). ``RuntimeEventSpanProcessor`` normalizes them in real time. Events are
printed locally, so no collector is required.
"""

from __future__ import annotations

from opentelemetry.sdk._logs.export import (
    LogRecordExporter,
    LogRecordExportResult,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind

from ioa_observe.sdk.tracing import RuntimeEventSpanProcessor


class PrintExporter(LogRecordExporter):
    def export(self, batch):
        for record in batch:
            attrs = record.log_record.attributes
            print(
                f"{record.log_record.event_name:<22} "
                f"session={attrs.get('session.id', '-')} "
                f"agent={attrs.get('agent.name', '-')} "
                f"tool={attrs.get('tool.name', '-')} "
                f"llm={attrs.get('llm.name', '-')}"
            )
        return LogRecordExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def traceloop_spans(tracer) -> None:
    with tracer.start_as_current_span(
        "planner.agent",
        kind=SpanKind.INTERNAL,
        attributes={
            "traceloop.span.kind": "agent",
            "traceloop.entity.name": "traceloop-planner",
            "traceloop.association.properties.session_id": "traceloop-session",
        },
    ):
        with tracer.start_as_current_span(
            "search.tool",
            kind=SpanKind.INTERNAL,
            attributes={
                "traceloop.span.kind": "tool",
                "traceloop.entity.name": "search",
                "traceloop.entity.input": '{"q": "weather"}',
                "traceloop.association.properties.session_id": "traceloop-session",
            },
        ):
            pass


def openinference_spans(tracer) -> None:
    with tracer.start_as_current_span(
        "oi-agent",
        kind=SpanKind.INTERNAL,
        attributes={"openinference.span.kind": "AGENT", "session.id": "oi-session"},
    ):
        with tracer.start_as_current_span(
            "ChatCompletion",
            kind=SpanKind.CLIENT,
            attributes={
                "openinference.span.kind": "LLM",
                "session.id": "oi-session",
                "llm.model_name": "gpt-4o",
                "llm.provider": "openai",
                "llm.input_messages.0.message.role": "user",
                "llm.input_messages.0.message.content": "Weather?",
            },
        ):
            pass


def langfuse_spans(tracer) -> None:
    with tracer.start_as_current_span(
        "lookup",
        kind=SpanKind.INTERNAL,
        attributes={
            "langfuse.observation.type": "tool",
            "langfuse.session.id": "langfuse-session",
            "langfuse.observation.input": '{"city": "London"}',
        },
    ):
        pass


def main() -> None:
    provider = TracerProvider()
    processor = RuntimeEventSpanProcessor(
        exporter=PrintExporter(),
        schedule_delay_millis=50,
    )
    provider.add_span_processor(processor)

    traceloop_spans(provider.get_tracer("opentelemetry.instrumentation.traceloop"))
    openinference_spans(provider.get_tracer("openinference.instrumentation"))
    langfuse_spans(provider.get_tracer("langfuse-sdk"))

    processor.force_flush()
    provider.shutdown()


if __name__ == "__main__":
    main()
