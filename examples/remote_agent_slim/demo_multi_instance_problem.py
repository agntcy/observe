#!/usr/bin/env python3
"""
Multi-Instance Tracking Problem Demonstration

This script demonstrates the current limitation where multiple instances
of the same agent cannot be distinguished in traces.

Scenario:
- Run 3 server instances (same agent type)
- Client sends messages that could be handled by any server
- Current tracing CANNOT tell which server instance handled each message

Run this to see the problem:
    python demo_multi_instance_problem.py
"""

import asyncio
import sys
from typing import List


# Mock SLIM for demonstration
class MockSlimApp:
    def __init__(self, identity: str):
        self.identity = identity
        # Each instance gets a unique ID (like real SLIM)
        import uuid

        self.id_str = str(uuid.uuid4())
        print(f"Created SLIM app: {identity}, instance ID: {self.id_str}")


class MockAgent:
    def __init__(self, agent_type: str, instance_num: int):
        self.agent_type = agent_type
        self.instance_num = instance_num
        self.slim_app = MockSlimApp(f"{agent_type}-{instance_num}")

        # THIS IS THE PROBLEM: All instances use the same service name
        self.service_name = agent_type  # ← No instance differentiation!

    async def process_message(self, message: str) -> str:
        """Simulate message processing"""
        await asyncio.sleep(0.1)
        return f"Processed by {self.agent_type} instance {self.instance_num}: {message}"


async def demonstrate_problem():
    """Demonstrate the multi-instance tracking problem"""

    print("=" * 80)
    print("MULTI-INSTANCE AGENT TRACKING PROBLEM DEMONSTRATION")
    print("=" * 80)
    print()

    # Create 3 server instances (same agent type)
    servers: List[MockAgent] = []
    for i in range(1, 4):
        server = MockAgent("executor-agent", i)
        servers.append(server)
        print(
            f"Server {i}: service.name='{server.service_name}', SLIM id={server.slim_app.id_str}"
        )

    print()
    print("-" * 80)
    print("CURRENT TRACING BEHAVIOR:")
    print("-" * 80)
    print()

    # Simulate message routing (round-robin)
    messages = ["Task A", "Task B", "Task C", "Task D", "Task E"]

    for idx, message in enumerate(messages):
        # Route to different servers (simulating SLIM topic-based routing)
        server = servers[idx % len(servers)]
        response = await server.process_message(message)

        print(f"Message: '{message}'")
        print(f"  → Handled by: instance {server.instance_num}")
        print(f"  → SLIM ID: {server.slim_app.id_str}")
        print(f"  → Response: {response}")
        print()

        # Show what appears in traces
        print("  📊 TRACE SPAN ATTRIBUTES (current implementation):")
        print("     gen_ai.agent.sender.id: 'client-agent'")
        print("     gen_ai.agent.receiver.id: 'executor-agent'  ← SAME FOR ALL!")
        print(f"     service.name: '{server.service_name}'  ← SAME FOR ALL!")
        print("     gen_ai.agent.communication.operation: 'request_reply'")
        print()
        print("  ❌ PROBLEM: Cannot tell which instance handled this message!")
        print()

    print("-" * 80)
    print("WHY THIS IS A PROBLEM:")
    print("-" * 80)
    print()
    print("1. All 3 server instances report the same service.name: 'executor-agent'")
    print("2. The unique SLIM id_str is NOT captured in spans")
    print("3. Traces show 'executor-agent' handled messages, but not WHICH instance")
    print("4. Cannot reconstruct message flow in multi-agent systems")
    print("5. Debugging and performance analysis is incomplete")
    print()

    print("-" * 80)
    print("WHAT SHOULD HAPPEN (PROPOSED SOLUTION):")
    print("-" * 80)
    print()
    print("📊 TRACE SPAN ATTRIBUTES (with instance tracking):")
    print()

    for idx, message in enumerate(messages):
        server = servers[idx % len(servers)]
        print(f"Message: '{message}'")
        print("  gen_ai.agent.sender.id: 'client-agent'")
        print("  gen_ai.agent.sender.instance_id: 'client-uuid-abc123'")
        print("  gen_ai.agent.receiver.id: 'executor-agent'")
        print(
            f"  gen_ai.agent.receiver.instance_id: '{server.slim_app.id_str[:8]}...'  ← UNIQUE!"
        )
        print("  service.name: 'executor-agent'")
        print(f"  service.instance.id: '{server.slim_app.id_str[:8]}...'  ← UNIQUE!")
        print()

    print("✅ BENEFIT: Now we can see exactly which instance handled each message!")
    print()


async def show_solution_code():
    """Show the code changes needed to fix this"""

    print("=" * 80)
    print("PROPOSED CODE CHANGES")
    print("=" * 80)
    print()

    print("1. UPDATE Observe.init() TO ACCEPT INSTANCE_ID:")
    print("-" * 80)
    print("""
# Current (client.py and server.py):
serviceName = "remote-server-agent"
Observe.init(serviceName, api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))

# Proposed:
serviceName = "remote-server-agent"
instance_id = os.getenv("INSTANCE_ID") or str(uuid.uuid4())[:8]
Observe.init(
    serviceName, 
    instance_id=instance_id,  # NEW parameter
    api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT")
)
    """)

    print()
    print("2. UPDATE SLIM INSTRUMENTATION TO CAPTURE INSTANCE ID:")
    print("-" * 80)
    print("""
# In slim.py instrumented_publish():
async def instrumented_publish(self, *args, **kwargs):
    if _global_tracer:
        with _global_tracer.start_as_current_span("gen_ai.agent.communication") as span:
            # ... existing code ...
            
            # NEW: Capture sender instance ID
            sender_instance_id = self.id_str if hasattr(self, 'id_str') else None
            
            set_communication_attributes(
                span=span,
                protocol="slim",
                sender_id=sender_id,
                sender_instance_id=sender_instance_id,  # NEW
                receiver_id=receiver_id,
                # ... other params
            )
    """)

    print()
    print("3. UPDATE MESSAGE HEADERS TO PROPAGATE INSTANCE ID:")
    print("-" * 80)
    print("""
# In slim.py - add to headers:
headers = {
    "session_id": session_id,
    "traceparent": traceparent,
    "sender_instance_id": self.id_str,  # NEW
    "sender_service_name": serviceName,  # NEW
}
    """)

    print()
    print("4. UPDATE RECEIVE PATH TO SET RECEIVER INSTANCE:")
    print("-" * 80)
    print("""
# In slim.py instrumented_receive():
headers = message_dict.get("headers", {})
sender_instance_id = headers.get("sender_instance_id")

# Set receiver instance (this agent's ID)
receiver_instance_id = self.id_str if hasattr(self, 'id_str') else None

# Restore or create span context with instance info
if _global_tracer:
    with _global_tracer.start_as_current_span("gen_ai.agent.communication") as span:
        span.set_attribute("gen_ai.agent.sender.instance_id", sender_instance_id)
        span.set_attribute("gen_ai.agent.receiver.instance_id", receiver_instance_id)
    """)

    print()
    print("5. UPDATE SEMANTIC CONVENTIONS:")
    print("-" * 80)
    print("""
# In agent_communication.py - add new attributes:
class AgentCommunicationAttributes:
    # ... existing attributes ...
    
    # NEW: Instance identification
    SENDER_INSTANCE_ID = "gen_ai.agent.sender.instance_id"
    RECEIVER_INSTANCE_ID = "gen_ai.agent.receiver.instance_id"
    
    # NEW: Service instance (OpenTelemetry standard)
    SERVICE_INSTANCE_ID = "service.instance.id"
    """)

    print()


async def show_deployment_examples():
    """Show deployment configuration examples"""

    print("=" * 80)
    print("DEPLOYMENT CONFIGURATION EXAMPLES")
    print("=" * 80)
    print()

    print("DOCKER COMPOSE:")
    print("-" * 80)
    print("""
services:
  executor-1:
    image: executor-agent:latest
    environment:
      - SERVICE_NAME=executor-agent
      - INSTANCE_ID=executor-1  # Unique per instance
      - OTLP_HTTP_ENDPOINT=http://otel-collector:4318
  
  executor-2:
    image: executor-agent:latest
    environment:
      - SERVICE_NAME=executor-agent
      - INSTANCE_ID=executor-2  # Unique per instance
      - OTLP_HTTP_ENDPOINT=http://otel-collector:4318
  
  executor-3:
    image: executor-agent:latest
    environment:
      - SERVICE_NAME=executor-agent
      - INSTANCE_ID=executor-3  # Unique per instance
      - OTLP_HTTP_ENDPOINT=http://otel-collector:4318
    """)

    print()
    print("KUBERNETES DEPLOYMENT:")
    print("-" * 80)
    print("""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: executor-agent
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: executor
        image: executor-agent:latest
        env:
        - name: SERVICE_NAME
          value: "executor-agent"
        - name: INSTANCE_ID
          valueFrom:
            fieldRef:
              fieldPath: metadata.name  # Use pod name as instance ID
        - name: OTLP_HTTP_ENDPOINT
          value: "http://otel-collector:4318"
    """)

    print()


async def main():
    """Run all demonstrations"""
    try:
        await demonstrate_problem()
        print("\n")
        await show_solution_code()
        print("\n")
        await show_deployment_examples()

        print("=" * 80)
        print("NEXT STEPS:")
        print("=" * 80)
        print()
        print("1. Review the analysis in docs/MULTI_AGENT_INSTANCE_TRACKING.md")
        print("2. Discuss the proposed solutions with the team")
        print("3. Decide on semantic convention attribute names")
        print("4. Implement changes to Observe.init() and instrumentation")
        print("5. Update examples to demonstrate instance tracking")
        print("6. Test with real multi-instance deployments")
        print()

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
