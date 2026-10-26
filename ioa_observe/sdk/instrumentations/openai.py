# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

"""Capture tool definitions from the OpenAI SDK's serialized model requests."""

import json
import logging

from opentelemetry import trace

from ioa_observe.sdk.config import (
    is_realtime_observability_enabled,
    is_tracing_enabled,
)
from ioa_observe.sdk.tracing.capability_capture import record_capability_event
from ioa_observe.sdk.tracing.runtime_events import RuntimeEventName

_logger = logging.getLogger(__name__)
_PRESENTATION_FIELDS = (
    "tools",
    "functions",
    "tool_choice",
    "function_call",
    "parallel_tool_calls",
)


def instrument_openai_tool_presentations() -> None:
    import openai

    if not hasattr(openai, "OpenAI"):
        _logger.warning("OpenAI tool presentation capture requires openai >= 1.0")
        return

    from openai._base_client import BaseClient
    from wrapt import wrap_function_wrapper

    from ioa_observe.sdk.tracing.tracing import is_llm_span

    if getattr(BaseClient._build_request, "_observe_tools_presented", False):
        return

    def build_request(wrapped, instance, args, kwargs):
        request = wrapped(*args, **kwargs)
        if not is_tracing_enabled() or not is_realtime_observability_enabled():
            return request
        try:
            path = request.url.path.rstrip("/")
            operation = next(
                (
                    operation
                    for suffix, operation in (
                        ("/chat/completions", "chat"),
                        ("/completions", "text_completion"),
                        ("/responses", "responses"),
                    )
                    if path.endswith(suffix)
                ),
                None,
            )
            if operation is None or request.method != "POST":
                return request
            # Read the built body so extra_body overrides and SDK transformations
            # are included, without retaining prompts, headers, or credentials.
            body = json.loads(request.content)
            if not isinstance(body, dict) or not {"tools", "functions"} & body.keys():
                return request
            content = {key: body[key] for key in _PRESENTATION_FIELDS if key in body}
            span = trace.get_current_span()
            context = span.get_span_context()
            span_attributes = getattr(span, "attributes", None) or {}
            attributes = {
                "operation.name": operation,
                "llm.name": body.get("model"),
                "llm.call.id": (
                    f"{context.span_id:016x}"
                    if context.is_valid and span_attributes and is_llm_span(span)
                    else None
                ),
            }
            record_capability_event(
                RuntimeEventName.LLM_TOOLS_PRESENTED.value,
                content,
                source="llm-presented",
                session_id=span_attributes.get("session.id"),
                attributes=attributes,
            )
        except Exception:
            _logger.warning("Unable to capture OpenAI tool presentation")
        return request

    wrap_function_wrapper(
        "openai._base_client", "BaseClient._build_request", build_request
    )
    BaseClient._build_request._observe_tools_presented = True
