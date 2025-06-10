# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import os


def is_tracing_enabled() -> bool:
    return (os.getenv("OBSERVE_TRACING_ENABLED") or "true").lower() == "true"


def is_content_tracing_enabled() -> bool:
    return (os.getenv("OBSERVE_TRACE_CONTENT") or "true").lower() == "true"


def is_metrics_enabled() -> bool:
    return (os.getenv("OBSERVE_METRICS_ENABLED") or "true").lower() == "true"


def is_logging_enabled() -> bool:
    return (os.getenv("OBSERVE_LOGGING_ENABLED") or "false").lower() == "true"
