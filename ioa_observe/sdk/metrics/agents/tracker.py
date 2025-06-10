# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from opentelemetry.metrics import get_meter


class AgentConnectionTracker:
    """Track connection reliability between agents"""

    def __init__(self):
        self.connections = {}  # {(sender, receiver): (success_count, total_count)}
        self.meter = get_meter("observe.metrics")

    def record_connection(self, sender: str, receiver: str, success: bool):
        """Record a connection attempt between agents"""
        key = (sender, receiver)
        if key not in self.connections:
            self.connections[key] = [0, 0]

        # Update counts
        self.connections[key][1] += 1  # total count
        if success:
            self.connections[key][0] += 1  # success count

        # Calculate reliability
        success_count, total_count = self.connections[key]
        reliability = success_count / total_count if total_count > 0 else 0.0

        # Record metric
        self.meter.create_gauge(
            name="gen_ai.ioa.agent.connection_reliability",
            description="Reliability of connections between agents",
            unit="1",
        ).set(reliability, {"sender_agent": sender, "receiver_agent": receiver})

        return reliability


# Create a singleton instance
connection_tracker = AgentConnectionTracker()
