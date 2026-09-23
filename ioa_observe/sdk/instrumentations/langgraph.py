# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ioa_observe.sdk.tracing.handoffs import HandoffSignal


def extract_langgraph_handoff(result: Any) -> HandoffSignal | None:
    result_type = type(result)
    is_langgraph_command = (
        result_type.__name__ == "Command"
        and result_type.__module__.startswith("langgraph.")
    )
    is_legacy_command_dict = isinstance(result, Mapping) and "goto" in result
    if not is_langgraph_command and not is_legacy_command_dict:
        return None

    target = (
        getattr(result, "goto", None) if is_langgraph_command else result.get("goto")
    )
    if not isinstance(target, str) or target in {"", "__end__"}:
        return None

    return HandoffSignal(
        target_agent=target,
        evidence=("framework:langgraph" if is_langgraph_command else "legacy:goto"),
        confidence=0.9 if is_langgraph_command else 0.6,
    )
