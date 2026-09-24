# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


_ERROR_CONTENT_PATTERNS = (
    re.compile(r"^\s*(?:error|exception|failed|failure)\s*[:\-]", re.IGNORECASE),
    re.compile(
        r"\b(?:file|directory|path|resource|record|document|tool|command|endpoint|url)"
        r"\b.{0,200}\bnot found\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:permission|access) denied\b", re.IGNORECASE),
    re.compile(r"\btimed? out\b", re.IGNORECASE),
)


def tool_error_message(result: Any) -> str | None:
    if isinstance(result, str):
        try:
            structured_result = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            structured_result = None
        if isinstance(structured_result, (Mapping, list)):
            return tool_error_message(structured_result)
        return (
            result
            if any(pattern.search(result) for pattern in _ERROR_CONTENT_PATTERNS)
            else None
        )

    if isinstance(result, Mapping):
        explicit_error = result.get("error")
        if explicit_error:
            return str(explicit_error)
        content = result.get("content")
        if content is not None:
            return tool_error_message(content)
        if result.get("type") == "text":
            return tool_error_message(result.get("text"))
        return None

    content = getattr(result, "content", None)
    if content is not None:
        return tool_error_message(content)

    if isinstance(result, Sequence):
        for item in result:
            error_message = tool_error_message(item)
            if error_message:
                return error_message

    return None
