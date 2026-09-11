# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

"""Instrument GenAI work with an application-owned OpenTelemetry SDK."""

from __future__ import annotations

import json
import os

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Tracer

from ioa_observe.sdk.tracing import (
    RuntimeEventSpanProcessor,
)


def run_weather_agent(tracer: Tracer) -> None:
    conversation_id = "conversation-123"
    with tracer.start_as_current_span(
        "invoke_agent Weather Agent",
        kind=SpanKind.INTERNAL,
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.conversation.id": conversation_id,
            "gen_ai.agent.name": "Weather Agent",
        },
    ):
        with tracer.start_as_current_span(
            "chat gpt-5",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "openai",
                "gen_ai.conversation.id": conversation_id,
                "gen_ai.request.model": "gpt-5",
                "gen_ai.input.messages": json.dumps(
                    [
                        {
                            "role": "user",
                            "parts": [{"type": "text", "content": "Weather?"}],
                        }
                    ]
                ),
            },
        ) as model_span:
            model_span.set_attribute("gen_ai.response.model", "gpt-5")
            model_span.set_attribute("gen_ai.response.id", "response-123")
            model_span.set_attribute(
                "gen_ai.output.messages",
                json.dumps(
                    [
                        {
                            "role": "assistant",
                            "parts": [
                                {
                                    "type": "tool_call",
                                    "id": "call-123",
                                    "name": "get_weather",
                                }
                            ],
                        }
                    ]
                ),
            )

        with tracer.start_as_current_span(
            "execute_tool get_weather",
            kind=SpanKind.INTERNAL,
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.conversation.id": conversation_id,
                "gen_ai.agent.name": "Weather Agent",
                "gen_ai.tool.name": "get_weather",
                "gen_ai.tool.call.id": "call-123",
                "gen_ai.tool.call.arguments": json.dumps({"city": "London"}),
            },
        ) as tool_span:
            tool_span.set_attribute(
                "gen_ai.tool.call.result",
                json.dumps({"temperature_celsius": 18, "conditions": "cloudy"}),
            )


def main() -> None:
    collector_endpoint = os.getenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "http://localhost:4318",
    ).rstrip("/")
    logs_endpoint = os.getenv(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        f"{collector_endpoint}/v1/logs",
    )
    traces_endpoint = os.getenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        f"{collector_endpoint}/v1/traces",
    )
    resource = Resource.create(
        {
            "service.name": "third-party-weather-agent",
            "service.version": "1.0.0",
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        RuntimeEventSpanProcessor(
            endpoint=logs_endpoint,
            resource=resource,
        )
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=traces_endpoint),
            schedule_delay_millis=5_000,
        )
    )

    run_weather_agent(provider.get_tracer("example.third_party_genai", "1.0.0"))
    provider.shutdown()


if __name__ == "__main__":
    main()
