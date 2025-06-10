# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import threading
import time

from opentelemetry.metrics import get_meter


class ConnectionReliabilityMetric:
    """Compute and record connection reliability between agents"""

    def __init__(self):
        self.meter = get_meter("observe.metrics")
        self.connections = {}  # {(sender, receiver): (successes, attempts)}
        self.lock = threading.RLock()  # Add a lock for thread safety
        self.gauge = self.meter.create_gauge(
            name="gen_ai.ioa.agent.connection_reliability",
            description="Reliability of connections between agents (0.0-1.0)",
            unit="1",
        )
        self.reliability_gauge = self.meter.create_gauge(
            name="gen_ai.ioa.agent.overall_reliability",
            description="Overall reliability of an agent (0.0-1.0)",
            unit="1",
        )

        # Start background thread for periodic reporting
        self._start_reporting_thread()

    def _start_reporting_thread(self):
        """Start background thread to report reliability metrics periodically"""

        def report_periodically():
            while True:
                self.report_reliability_metrics()
                time.sleep(60)  # Report every minute

        thread = threading.Thread(target=report_periodically, daemon=True)
        thread.start()

    def report_reliability_metrics(self):
        """Report reliability metrics for all agents"""
        with self.lock:
            # Get unique set of agents from all connections
            all_agents = set()
            for sender, receiver in self.connections.keys():
                all_agents.add(sender)
                if receiver not in self.connections:
                    all_agents.add(receiver)

            # Calculate and report overall reliability for each agent
            for agent_name in all_agents:
                self.get_agent_reliability(agent_name)
                # The get_agent_reliability method already sets the gauge
                # for the agent, so we don't need to do it here again.

    def record_connection_attempt(
        self, sender: str, receiver: str, success: bool = True
    ) -> float:
        """
        Record connection attempt between agents and return reliability score

        Args:
            sender: Name of sending agent
            receiver: Name of receiving agent
            success: Whether the connection was successful

        Returns:
            float: Current reliability score (0.0-1.0)
        """
        key = (sender, receiver)

        # Initialize if first time seeing this connection
        if key not in self.connections:
            self.connections[key] = [0, 0]

        # Update counts
        successes, attempts = self.connections[key]
        attempts += 1
        if success:
            successes += 1
        self.connections[key] = [successes, attempts]

        # Calculate reliability
        reliability = successes / attempts if attempts > 0 else 0.0

        # Record metric
        self.gauge.set(
            reliability,
            {
                "sender_agent": sender,
                "receiver_agent": receiver,
            },
        )

        return reliability

    def get_agent_reliability(self, agent_name: str) -> float:
        """
        Get overall reliability for a specific agent

        Args:
            agent_name: Name of the agent

        Returns:
            float: Overall reliability score (0.0-1.0)
        """
        with self.lock:  # Add lock for thread safety
            total_successes = 0
            total_attempts = 0

            # Sum all connections where agent is sender
            for (sender, _), (successes, attempts) in self.connections.items():
                if sender == agent_name:
                    total_successes += successes
                    total_attempts += attempts

            reliability = (
                total_successes / total_attempts if total_attempts > 0 else 0.0
            )

            # Record overall agent reliability
            self.reliability_gauge.set(reliability, {"agent_name": agent_name})

            return reliability


# Singleton instance
connection_reliability = ConnectionReliabilityMetric()
