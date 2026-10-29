# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

"""Shared serialization and content controls for capability definition events."""

import json
import logging
import os
import re
from urllib.parse import urlsplit, urlunsplit

from opentelemetry.context import get_value

from ioa_observe.sdk.config import is_content_tracing_enabled, is_tracing_enabled
from ioa_observe.sdk.tracing.runtime_events import build_runtime_event_attributes
from ioa_observe.sdk.tracing.runtime_event_emitter import emit_runtime_event

_logger = logging.getLogger(__name__)
_SECRET_FIELDS = {
    "authorization",
    "credentials",
    "apikey",
    "accesstoken",
    "refreshtoken",
    "clientsecret",
    "password",
    "token",
    "secret",
    "cookie",
}


def safe_provider_url(value):
    if not isinstance(value, str) or not value:
        return None
    parsed = urlsplit(value)
    if not parsed.netloc:
        return value
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc.rsplit("@", 1)[-1],
            parsed.path,
            "",
            "",
        )
    )


def _redact(value, *, schema=False):
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if not schema and re.sub(r"[^a-z]", "", key.lower()) in _SECRET_FIELDS
                else _redact(
                    item,
                    schema=schema
                    or key in {"inputSchema", "outputSchema", "parameters"},
                )
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, schema=schema) for item in value]
    if isinstance(value, str) and "://" in value and not schema:
        return safe_provider_url(value)
    return value


def build_capability_attributes(
    content,
    *,
    provider_id=None,
    source,
    attributes=None,
):
    """Build capability attributes, honoring content-tracing and size limits."""
    event_attributes = {"capability.source": source, **(attributes or {})}
    try:
        provider_id = safe_provider_url(provider_id)
        if provider_id:
            event_attributes["capability.provider.id"] = provider_id
        if is_content_tracing_enabled():
            if hasattr(content, "model_dump"):
                content = content.model_dump(mode="json", by_alias=True)
            redacted = _redact(content)
            payload = json.dumps(redacted, ensure_ascii=False, allow_nan=False)
            limit = int(os.getenv("OBSERVE_CAPABILITY_MAX_PAYLOAD_BYTES", "262144"))
            if limit <= 0:
                raise ValueError("Capability payload limit must be positive")
            if len(payload.encode("utf-8")) > limit:
                event_attributes["capability.capture.state"] = "truncated"
                event_attributes["capability.content.size"] = len(
                    payload.encode("utf-8")
                )
            else:
                event_attributes["capability.content"] = payload
                event_attributes["capability.capture.state"] = (
                    "redacted" if redacted != content else "complete"
                )
        else:
            event_attributes["capability.capture.state"] = "disabled"
    except Exception:
        _logger.warning("Unable to serialize capability definition")
        event_attributes["capability.capture.state"] = "unavailable"
    return event_attributes


def record_capability_event(
    name,
    content,
    *,
    provider_id=None,
    source,
    attributes=None,
    session_id=None,
):
    """Record full structured content without changing application behavior."""
    if not is_tracing_enabled():
        return
    event_attributes = build_capability_attributes(
        content, provider_id=provider_id, source=source, attributes=attributes
    )
    try:
        session_id = session_id or get_value("session.id")
        emit_runtime_event(
            build_runtime_event_attributes(
                name,
                session_id=session_id
                if isinstance(session_id, str) and session_id
                else None,
                snapshot_version=0,
                **event_attributes,
            )
        )
    except Exception:
        _logger.warning("Unable to emit capability definition event %s", name)
