# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from opentelemetry.metrics import get_meter
import time
import threading
from collections import defaultdict


class ToolCallTracker:
    """Track tool/API call success and failure rates"""

    def __init__(self):
        self.meter = get_meter("observe.metrics")
        self.tool_calls = defaultdict(lambda: {"success": 0, "failure": 0})
        self.lock = threading.RLock()

        # Start background thread for periodic reporting
        self._start_reporting_thread()

    def _start_reporting_thread(self):
        """Start background thread to report metrics periodically"""

        def report_periodically():
            while True:
                self.report_metrics()
                time.sleep(60)  # Report every minute

        thread = threading.Thread(target=report_periodically, daemon=True)
        thread.start()

    def record_tool_call(self, tool_name: str, success: bool, error_type: str = None):
        """
        Record a tool/API call attempt

        Args:
            tool_name: Name of the tool or API being called
            success: Whether the call succeeded
            error_type: Type of error if the call failed
        """
        with self.lock:
            if success:
                self.tool_calls[tool_name]["success"] += 1
            else:
                self.tool_calls[tool_name]["failure"] += 1

            # Record metric immediately
            self.meter.create_counter(
                name="gen_ai.ioa.tool.call_count",
                description="Number of tool/API calls",
                unit="1",
            ).add(
                1,
                {
                    "tool_name": tool_name,
                    "status": "success" if success else "failure",
                    "error_type": error_type if error_type else "none",
                },
            )

    def get_failure_rate(self, tool_name: str) -> float:
        """
        Calculate failure rate for a specific tool

        Args:
            tool_name: Name of the tool or API

        Returns:
            float: Failure rate (0.0-1.0)
        """
        with self.lock:
            if tool_name not in self.tool_calls:
                return 0.0

            stats = self.tool_calls[tool_name]
            total = stats["success"] + stats["failure"]

            if total == 0:
                return 0.0

            return stats["failure"] / total

    def report_metrics(self):
        """Report failure rate metrics for all tracked tools"""
        with self.lock:
            for tool_name, stats in self.tool_calls.items():
                total = stats["success"] + stats["failure"]
                if total > 0:
                    failure_rate = stats["failure"] / total

                    self.meter.create_gauge(
                        name="gen_ai.ioa.tool.failure_rate",
                        description="Failure rate of tool/API calls",
                        unit="1",
                    ).set(failure_rate, {"tool_name": tool_name})


# Create singleton instance
tool_call_tracker = ToolCallTracker()
