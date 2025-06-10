# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from opentelemetry.metrics import get_meter
import time
import threading


class AgentAvailabilityTracker:
    """Track agent uptime and availability metrics"""

    def __init__(self, availability_window_seconds=300):
        self.meter = get_meter("observe.metrics")
        self.window_seconds = availability_window_seconds
        self.agent_activity = {}  # {agent_name: [(timestamp, success_bool), ...]}
        self.agent_heartbeats = {}  # {agent_name: last_heartbeat_timestamp}
        self.lock = threading.RLock()  # Thread-safe operations

        # Start background thread for periodic reporting
        self._start_reporting_thread()

    def _start_reporting_thread(self):
        """Start background thread to report availability metrics"""

        def report_periodically():
            while True:
                self.report_availability_metrics()
                time.sleep(60)  # Report every minute

        thread = threading.Thread(target=report_periodically, daemon=True)
        thread.start()

    def record_agent_activity(self, agent_name: str, success: bool = True):
        """
        Record agent activity (successful or failed operation)

        Args:
            agent_name: Name of the agent
            success: Whether the operation was successful
        """
        timestamp = time.time()

        with self.lock:
            if agent_name not in self.agent_activity:
                self.agent_activity[agent_name] = []

            # Add new activity and maintain window
            self.agent_activity[agent_name].append((timestamp, success))

            # Remove activities outside the window
            cutoff = timestamp - self.window_seconds
            self.agent_activity[agent_name] = [
                entry for entry in self.agent_activity[agent_name] if entry[0] >= cutoff
            ]

            # Update heartbeat
            self.agent_heartbeats[agent_name] = timestamp

    def record_agent_heartbeat(self, agent_name: str):
        """
        Record a heartbeat to indicate agent is alive

        Args:
            agent_name: Name of the agent
        """
        with self.lock:
            self.agent_heartbeats[agent_name] = time.time()

    def get_agent_availability(self, agent_name: str) -> float:
        """
        Calculate agent availability as ratio of successful operations

        Args:
            agent_name: Name of the agent

        Returns:
            float: Availability score (0.0-1.0)
        """
        with self.lock:
            if agent_name not in self.agent_activity:
                return 0.0

            activities = self.agent_activity[agent_name]
            if not activities:
                return 0.0

            successful = sum(1 for _, success in activities if success)
            return successful / len(activities)

    def get_agent_uptime(self, agent_name: str) -> float:
        """
        Calculate agent uptime based on recent heartbeats

        Args:
            agent_name: Name of the agent

        Returns:
            float: Uptime score (0.0-1.0), 1.0 means fully operational
        """
        current_time = time.time()

        with self.lock:
            if agent_name not in self.agent_heartbeats:
                return 0.0

            last_heartbeat = self.agent_heartbeats[agent_name]
            time_since_heartbeat = current_time - last_heartbeat

            # Consider agent up if heartbeat within last 2 minutes
            if time_since_heartbeat <= 120:
                return 1.0

            # Linear degradation between 2-10 minutes
            if time_since_heartbeat <= 600:
                return 1.0 - ((time_since_heartbeat - 120) / 480)

            return 0.0  # Consider down after 10 minutes

    def report_availability_metrics(self):
        """Report availability metrics for all tracked agents"""
        # current_time = time.time()

        with self.lock:
            for agent_name in set(self.agent_activity.keys()) | set(
                self.agent_heartbeats.keys()
            ):
                # Calculate availability (success rate)
                availability = self.get_agent_availability(agent_name)

                # Calculate uptime (based on heartbeats)
                uptime = self.get_agent_uptime(agent_name)

                # Report metrics
                self.meter.create_gauge(
                    name="gen_ai.ioa.agent.availability",
                    description="Agent availability based on success rate",
                    unit="1",
                ).set(availability, {"agent_name": agent_name})

                self.meter.create_gauge(
                    name="gen_ai.ioa.agent.uptime",
                    description="Agent uptime based on recent activity",
                    unit="1",
                ).set(uptime, {"agent_name": agent_name})


# Singleton instance
agent_availability = AgentAvailabilityTracker()
