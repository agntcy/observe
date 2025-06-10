# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from opentelemetry.metrics import get_meter
import time
import threading
from collections import defaultdict


class AgentRecoveryTracker:
    """Track agent failures and recoveries to calculate recovery rate"""

    def __init__(self, recovery_window_seconds=3600):  # 1 hour window
        self.meter = get_meter("observe.metrics")
        self.lock = threading.RLock()
        self.recovery_window = recovery_window_seconds

        # Track failures and recoveries within a sliding window
        self.agent_failures = defaultdict(
            list
        )  # {agent_name: [(timestamp, error_type), ...]}
        self.agent_recoveries = defaultdict(list)  # {agent_name: [timestamp, ...]}

        # Start background thread for periodic reporting
        self._start_reporting_thread()

    def _start_reporting_thread(self):
        """Start background thread to report recovery metrics periodically"""

        def report_periodically():
            while True:
                self.report_recovery_metrics()
                time.sleep(60)  # Report every minute

        thread = threading.Thread(target=report_periodically, daemon=True)
        thread.start()

    def record_agent_failure(self, agent_name: str, error_type: str = "unknown"):
        """
        Record an agent failure event

        Args:
            agent_name: Name of the failed agent
            error_type: Type of error that occurred
        """
        with self.lock:
            timestamp = time.time()
            self.agent_failures[agent_name].append((timestamp, error_type))

            # Prune old failures outside window
            self._prune_old_events()

    def record_agent_recovery(self, agent_name: str):
        """
        Record that an agent successfully recovered after a failure

        Args:
            agent_name: Name of the recovered agent
        """
        with self.lock:
            timestamp = time.time()
            self.agent_recoveries[agent_name].append(timestamp)

            # Prune old recoveries outside window
            self._prune_old_events()

    def _prune_old_events(self):
        """Remove events outside the recovery window"""
        cutoff = time.time() - self.recovery_window

        for agent in self.agent_failures:
            self.agent_failures[agent] = [
                failure
                for failure in self.agent_failures[agent]
                if failure[0] >= cutoff
            ]

        for agent in self.agent_recoveries:
            self.agent_recoveries[agent] = [
                recovery
                for recovery in self.agent_recoveries[agent]
                if recovery >= cutoff
            ]

    def get_agent_recovery_rate(self, agent_name: str) -> float:
        """
        Calculate recovery rate for an agent (recoveries / failures)

        Args:
            agent_name: Name of the agent

        Returns:
            float: Recovery rate (0.0-1.0)
        """
        with self.lock:
            if agent_name not in self.agent_failures:
                return 1.0  # No failures means perfect recovery rate

            failures = len(self.agent_failures[agent_name])
            if failures == 0:
                return 1.0

            recoveries = len(self.agent_recoveries.get(agent_name, []))
            return min(recoveries / failures, 1.0)  # Cap at 1.0

    def report_recovery_metrics(self):
        """Report recovery metrics for all tracked agents"""
        with self.lock:
            for agent_name in set(self.agent_failures.keys()):
                recovery_rate = self.get_agent_recovery_rate(agent_name)

                # Record recovery rate metric
                self.meter.create_gauge(
                    name="gen_ai.ioa.agent.recovery_rate",
                    description="Agent recovery rate after failures",
                    unit="1",
                ).set(recovery_rate, {"agent_name": agent_name})

                # Record count metrics
                failures = len(self.agent_failures[agent_name])
                recoveries = len(self.agent_recoveries.get(agent_name, []))

                self.meter.create_gauge(
                    name="gen_ai.ioa.agent.failures_count",
                    description="Number of agent failures in window",
                    unit="1",
                ).set(failures, {"agent_name": agent_name})

                self.meter.create_gauge(
                    name="gen_ai.ioa.agent.recoveries_count",
                    description="Number of agent recoveries in window",
                    unit="1",
                ).set(recoveries, {"agent_name": agent_name})


# Create singleton instance
agent_recovery_tracker = AgentRecoveryTracker()
