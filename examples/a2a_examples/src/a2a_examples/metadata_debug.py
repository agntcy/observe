import json
from collections.abc import Mapping
from typing import Any

from a2a.client.interceptors import AfterArgs, BeforeArgs, ClientCallInterceptor
from a2a.server.agent_execution import RequestContext
from a2a.types import SendMessageRequest
from google.protobuf.json_format import MessageToDict
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


def print_request_metadata(
    metadata: Mapping[str, Any],
    *,
    direction: str,
    operation: str,
    metadata_location: str = "request.metadata",
) -> None:
    observe = metadata.get("observe", {})
    traceparent = observe.get("traceparent") if isinstance(observe, Mapping) else None
    carrier = {"traceparent": traceparent} if isinstance(traceparent, str) else {}
    # Decode the wire value, not the span currently active in this process.
    context = TraceContextTextMapPropagator().extract(carrier)
    span_context = trace.get_current_span(context).get_span_context()
    print(
        json.dumps(
            {
                "direction": direction,
                "operation": operation,
                "metadata_location": metadata_location,
                "metadata": dict(metadata),
                "traceparent": traceparent,
                "trace_context_status": (
                    "valid"
                    if span_context.is_valid
                    else "invalid"
                    if traceparent
                    else "missing"
                ),
                "trace_id": (
                    f"{span_context.trace_id:032x}" if span_context.is_valid else None
                ),
                "span_id": (
                    f"{span_context.span_id:016x}" if span_context.is_valid else None
                ),
            },
            indent=2,
        ),
        flush=True,
    )


def print_incoming_metadata(context: RequestContext) -> None:
    message_metadata = (
        MessageToDict(context.message.metadata) if context.message is not None else {}
    )
    print_request_metadata(
        message_metadata or context.metadata,
        direction="incoming",
        operation="agent.execute",
        metadata_location=(
            "message.metadata" if message_metadata else "request.metadata"
        ),
    )


class RequestMetadataPrinter(ClientCallInterceptor):
    async def before(self, args: BeforeArgs) -> None:
        if not isinstance(args.input, SendMessageRequest):
            return
        message_metadata = MessageToDict(args.input.message.metadata)
        print_request_metadata(
            message_metadata or MessageToDict(args.input.metadata),
            direction="outgoing",
            operation=args.method,
            metadata_location=(
                "message.metadata" if message_metadata else "request.metadata"
            ),
        )

    async def after(self, args: AfterArgs) -> None:
        pass
