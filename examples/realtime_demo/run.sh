#!/usr/bin/env bash
# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

cd "$(dirname "$0")"

# Runs the in-process real-time observability demo. No external collector or
# ClickHouse is required: pushed runtime events are materialized in-process.
if command -v uv >/dev/null 2>&1; then
  exec uv run --project ../.. python agents.py
else
  exec python agents.py
fi
