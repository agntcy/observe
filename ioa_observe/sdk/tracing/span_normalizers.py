# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

"""Rewrite vendor-specific span attributes into canonical ``gen_ai.*`` keys.

Normalizers only add keys that are missing; existing ``gen_ai.*`` values are
never overwritten, so spans that already follow the semantic conventions pass
through unchanged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

SESSION_ID = "gen_ai.conversation.id"
OPERATION = "gen_ai.operation.name"
AGENT_NAME = "gen_ai.agent.name"
TOOL_NAME = "gen_ai.tool.name"
TOOL_ARGS = "gen_ai.tool.call.arguments"
TOOL_RESULT = "gen_ai.tool.call.result"
PROVIDER = "gen_ai.provider.name"
REQUEST_MODEL = "gen_ai.request.model"
INPUT_MESSAGES = "gen_ai.input.messages"
OUTPUT_MESSAGES = "gen_ai.output.messages"

_UNKNOWN_PROVIDER = "unknown"


class SpanAttributeNormalizer(Protocol):
    def __call__(
        self,
        attributes: Mapping[str, Any],
        span_name: str,
    ) -> Mapping[str, Any]:
        """Return canonical attributes to add; must not mutate ``attributes``."""
        ...


def _text(attributes: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = attributes.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _indexed_messages(
    attributes: Mapping[str, Any], prefix: str, suffix: str
) -> str | None:
    """Collect ``<prefix>.N<suffix>.role|content`` attributes into a JSON list."""
    pattern = re.compile(
        rf"^{re.escape(prefix)}\.(\d+){re.escape(suffix)}\.(role|content)$"
    )
    messages: dict[int, dict[str, Any]] = {}
    for key, value in attributes.items():
        match = pattern.match(key)
        if match:
            messages.setdefault(int(match.group(1)), {})[match.group(2)] = value
    if not messages:
        return None
    return json.dumps([messages[index] for index in sorted(messages)])


def _finish(
    attributes: Mapping[str, Any],
    additions: dict[str, Any],
) -> dict[str, Any]:
    result = {
        key: value
        for key, value in additions.items()
        if value is not None and key not in attributes
    }
    effective_operation = attributes.get(OPERATION) or result.get(OPERATION)
    if (
        effective_operation in {"chat", "text_completion", "generate_content"}
        and not attributes.get(PROVIDER)
        and PROVIDER not in result
    ):
        result[PROVIDER] = _UNKNOWN_PROVIDER
    return result


def normalize_traceloop(
    attributes: Mapping[str, Any],
    span_name: str,
) -> Mapping[str, Any]:
    """Traceloop / OpenLLMetry (``traceloop.*`` and ``llm.request.type``)."""
    kind = _text(attributes, "traceloop.span.kind")
    request_type = _text(attributes, "llm.request.type")
    if kind is None and request_type is None:
        if not any(key.startswith("traceloop.") for key in attributes):
            return {}

    add: dict[str, Any] = {
        SESSION_ID: _text(
            attributes,
            "traceloop.association.properties.session_id",
            "traceloop.association.properties.conversation_id",
            "traceloop.association.properties.thread_id",
        ),
        PROVIDER: _text(attributes, "gen_ai.system"),
    }
    entity = _text(attributes, "traceloop.entity.name") or span_name
    if kind == "agent":
        add[OPERATION] = "invoke_agent"
        add[AGENT_NAME] = entity
        add[INPUT_MESSAGES] = _text(attributes, "traceloop.entity.input")
        add[OUTPUT_MESSAGES] = _text(attributes, "traceloop.entity.output")
    elif kind == "tool":
        add[OPERATION] = "execute_tool"
        add[TOOL_NAME] = entity
        add[TOOL_ARGS] = _text(attributes, "traceloop.entity.input")
        add[TOOL_RESULT] = _text(attributes, "traceloop.entity.output")
    elif request_type is not None:
        add[OPERATION] = {
            "chat": "chat",
            "completion": "text_completion",
            "embedding": "embeddings",
        }.get(request_type)
        add[INPUT_MESSAGES] = _indexed_messages(attributes, "gen_ai.prompt", "")
        add[OUTPUT_MESSAGES] = _indexed_messages(attributes, "gen_ai.completion", "")
    return _finish(attributes, add)


def normalize_openinference(
    attributes: Mapping[str, Any],
    span_name: str,
) -> Mapping[str, Any]:
    """OpenInference (Arize Phoenix and compatible instrumentors)."""
    kind = _text(attributes, "openinference.span.kind")
    if kind is None:
        return {}

    add: dict[str, Any] = {
        SESSION_ID: _text(attributes, "session.id"),
        PROVIDER: _text(attributes, "llm.provider", "llm.system"),
    }
    if kind == "AGENT":
        add[OPERATION] = "invoke_agent"
        add[AGENT_NAME] = _text(attributes, "agent.name") or span_name
        add[INPUT_MESSAGES] = _text(attributes, "input.value")
        add[OUTPUT_MESSAGES] = _text(attributes, "output.value")
    elif kind == "TOOL":
        add[OPERATION] = "execute_tool"
        add[TOOL_NAME] = _text(attributes, "tool.name") or span_name
        add[TOOL_ARGS] = _text(attributes, "input.value")
        add[TOOL_RESULT] = _text(attributes, "output.value")
    elif kind == "LLM":
        add[OPERATION] = "chat"
        add[REQUEST_MODEL] = _text(attributes, "llm.model_name")
        add[INPUT_MESSAGES] = _indexed_messages(
            attributes, "llm.input_messages", ".message"
        )
        add[OUTPUT_MESSAGES] = _indexed_messages(
            attributes, "llm.output_messages", ".message"
        )
    return _finish(attributes, add)


def normalize_langfuse(
    attributes: Mapping[str, Any],
    span_name: str,
) -> Mapping[str, Any]:
    """Langfuse v3+ OTel attributes (``langfuse.*``)."""
    kind = _text(attributes, "langfuse.observation.type")
    if kind is None:
        return {}

    add: dict[str, Any] = {
        SESSION_ID: _text(attributes, "langfuse.session.id", "session.id"),
        PROVIDER: _text(attributes, "gen_ai.system"),
    }
    observation_input = _text(attributes, "langfuse.observation.input")
    observation_output = _text(attributes, "langfuse.observation.output")
    if kind == "agent":
        add[OPERATION] = "invoke_agent"
        add[AGENT_NAME] = span_name
        add[INPUT_MESSAGES] = observation_input
        add[OUTPUT_MESSAGES] = observation_output
    elif kind == "tool":
        add[OPERATION] = "execute_tool"
        add[TOOL_NAME] = span_name
        add[TOOL_ARGS] = observation_input
        add[TOOL_RESULT] = observation_output
    elif kind == "generation":
        add[OPERATION] = "chat"
        add[REQUEST_MODEL] = _text(attributes, "langfuse.observation.model.name")
        add[INPUT_MESSAGES] = observation_input
        add[OUTPUT_MESSAGES] = observation_output
    return _finish(attributes, add)


def normalize_session_fallback(
    attributes: Mapping[str, Any],
    span_name: str,
) -> Mapping[str, Any]:
    """Fill the session id from common vendor-neutral keys when still missing."""
    if attributes.get(SESSION_ID):
        return {}
    value = _text(attributes, "session.id", "thread_id", "conversation_id")
    return {SESSION_ID: value} if value else {}


DEFAULT_NORMALIZERS: tuple[SpanAttributeNormalizer, ...] = (
    normalize_traceloop,
    normalize_openinference,
    normalize_langfuse,
    normalize_session_fallback,
)


def normalize_span_attributes(
    attributes: Mapping[str, Any],
    span_name: str = "",
    normalizers: Sequence[SpanAttributeNormalizer] = DEFAULT_NORMALIZERS,
) -> dict[str, Any]:
    """Return ``attributes`` plus canonical keys; originals always win."""
    merged = dict(attributes)
    for normalizer in normalizers:
        for key, value in normalizer(merged, span_name).items():
            merged.setdefault(key, value)
    return merged
