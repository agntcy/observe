"""
Example: What traces look like in observability UIs

This example demonstrates the actual trace output structure you'll see
when exporting to Langfuse, Grafana, Jaeger, etc.
"""

import asyncio
from ioa_observe.sdk import Observe


# This is what your instrumented code looks like (no changes needed)
async def example_multi_agent_workflow():
    """
    Example multi-agent workflow showing agent-to-agent communication.

    When this runs, it will generate traces with semantic conventions
    that appear in your observability UI.
    """
    # Agent 1: Planning Agent publishes task via SLIM
    # This creates a span: gen_ai.agent.communication
    slim_client = get_slim_client()
    session = await slim_client.create_session(config)

    task_message = {
        "task": "analyze_data",
        "priority": "high",
        "dataset": "customer_feedback_2024",
    }

    # This will create a span with these attributes:
    # - gen_ai.agent.communication.protocol: "slim"
    # - gen_ai.agent.communication.operation: "publish"
    # - gen_ai.agent.communication.message.size: 85 (bytes)
    # - gen_ai.agent.communication.message.type: "request"
    # - gen_ai.agent.communication.conversation.id: "session-abc123"
    # - gen_ai.agent.communication.status_code: "OK"
    # Event: message.sent (with message summary)
    await slim_client.publish(session, task_message, topic_name)

    # Agent 2: Execution Agent receives and processes
    # This creates a child span: gen_ai.agent.communication (receive)
    recv_session, received_message = await slim_client.receive(session)

    # The receive span will have:
    # Event: message.received (with message summary)

    # Agent 2 calls OpenAI (creates a gen_ai.chat.completions span)
    result = await openai.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": "Analyze: " + received_message}],
    )

    # Agent 2 forwards result to Memory Agent via A2A
    # This creates another span: gen_ai.agent.communication
    a2a_client = get_a2a_client()
    memory_request = {"action": "store", "data": result}

    # This span will have:
    # - gen_ai.agent.communication.protocol: "a2a"
    # - gen_ai.agent.communication.receiver.id: "memory-agent"
    # - gen_ai.agent.communication.operation: "send_message"
    # - gen_ai.agent.communication.message.type: "request"
    # - gen_ai.agent.communication.status_code: "OK"
    # Event: message.sent
    await a2a_client.send_message(memory_request)


# ==============================================================================
# WHAT YOU'LL SEE IN LANGFUSE
# ==============================================================================

"""
Langfuse Trace View:

📊 Trace: example_multi_agent_workflow
   Duration: 1.5s
   Status: ✅ Success
   Cost: $0.018

   └─ 🔵 gen_ai.agent.communication                    [150ms]
      Span ID: 7a8b9c0d
      Type: CLIENT
      
      Attributes:
      • protocol: slim
      • operation: publish
      • channel: acme/planning/tasks
      • message.size: 85 bytes
      • message.type: request
      • conversation.id: session-abc123
      • status_code: OK
      
      Events:
      ⬤ message.sent (0ms)
         message.size: 85
         message.summary: {"task": "analyze_data", "priority": "high"...
      
      └─ 🔵 gen_ai.agent.communication                 [80ms]
         Span ID: 8b9c0d1e
         Type: SERVER
         
         Attributes:
         • protocol: slim
         • conversation.id: session-abc123
         
         Events:
         ⬤ message.received (0ms)
            message.size: 85
         
         ├─ 🤖 gen_ai.chat.completions                 [1200ms]
         │  Span ID: 9c0d1e2f
         │  
         │  Attributes:
         │  • system: openai
         │  • model: gpt-4
         │  • input_tokens: 150
         │  • output_tokens: 300
         │  • completion_tokens: 300
         │  • prompt_tokens: 150
         │  
         │  💰 Cost: $0.015
         │  
         │  Messages:
         │  ├─ [user]: "Analyze: customer feedback..."
         │  └─ [assistant]: "Based on the analysis..."
         │
         └─ 🔵 gen_ai.agent.communication              [45ms]
            Span ID: 0d1e2f3g
            Type: CLIENT
            
            Attributes:
            • protocol: a2a
            • receiver.id: memory-agent
            • operation: send_message
            • message.size: 512 bytes
            • message.type: request
            • status_code: OK
            
            Events:
            ⬤ message.sent (0ms)
               message.size: 512

Total Metrics:
├─ 3 Agent Communications
├─ 1 LLM Call
├─ Total Duration: 1.5s
└─ Total Cost: $0.018
"""


# ==============================================================================
# WHAT YOU'LL SEE IN GRAFANA/TEMPO
# ==============================================================================

"""
Grafana Trace View (Flamegraph):

┌────────────────────────────────────────────────────────────────────────┐
│ Trace ID: 7af8c3d2e1b4a5f6c8d9e0f1a2b3c4d5                             │
│ Duration: 1.5s                                                          │
│ Services: planning-agent, execution-agent, memory-agent                 │
│ Spans: 4                                                                │
└────────────────────────────────────────────────────────────────────────┘

Timeline (0ms ──────────────────────────────────────────────────── 1500ms)

gen_ai.agent.communication (slim/publish)
█████ [150ms]
  ├─ protocol: slim
  ├─ operation: publish
  ├─ message.size: 85
  └─ status_code: OK

  gen_ai.agent.communication (slim/receive)
  ███ [80ms]
    ├─ protocol: slim
    └─ event: message.received
  
    gen_ai.chat.completions (openai/gpt-4)
    ████████████████████████████████████████████ [1200ms]
      ├─ model: gpt-4
      ├─ input_tokens: 150
      └─ output_tokens: 300
    
    gen_ai.agent.communication (a2a/send_message)
    ██ [45ms]
      ├─ protocol: a2a
      ├─ receiver.id: memory-agent
      └─ status_code: OK
"""


# ==============================================================================
# WHAT YOU'LL SEE IN JAEGER
# ==============================================================================

"""
Jaeger UI:

Service: planning-agent
Operation: gen_ai.agent.communication
Duration: 150ms
Trace ID: 7af8c3d2e1b4a5f6c8d9e0f1a2b3c4d5

┌─ Tags ────────────────────────────────────────────────────────────────┐
│ gen_ai.agent.communication.protocol         slim                      │
│ gen_ai.agent.communication.operation        publish                   │
│ gen_ai.agent.communication.channel          acme/planning/tasks       │
│ gen_ai.agent.communication.message.size     85                        │
│ gen_ai.agent.communication.message.type     request                   │
│ gen_ai.agent.communication.conversation.id  session-abc123            │
│ gen_ai.agent.communication.status_code      OK                        │
│ span.kind                                   client                    │
└───────────────────────────────────────────────────────────────────────┘

┌─ Events ──────────────────────────────────────────────────────────────┐
│ @ 0ms: message.sent                                                   │
│   - message.size: 85                                                  │
│   - message.summary: {"task": "analyze_data", "priority": "high"...   │
└───────────────────────────────────────────────────────────────────────┘

┌─ Process ─────────────────────────────────────────────────────────────┐
│ service.name        planning-agent                                    │
│ service.version     1.0.0                                             │
│ host.name           agent-node-01                                     │
└───────────────────────────────────────────────────────────────────────┘

Child Spans (1):
└─ gen_ai.agent.communication [execution-agent] 1.4s
"""


# ==============================================================================
# WHAT YOU'LL SEE IN HONEYCOMB
# ==============================================================================

"""
Honeycomb Query Interface:

┌─ Filters ──────────────────────────────────────────────────────────────┐
│ WHERE span.name = "gen_ai.agent.communication"                        │
│ AND gen_ai.agent.communication.protocol IN ["slim", "a2a"]            │
└───────────────────────────────────────────────────────────────────────┘

┌─ Breakdown ────────────────────────────────────────────────────────────┐
│ GROUP BY: gen_ai.agent.communication.protocol,                        │
│           gen_ai.agent.communication.status_code                      │
│ CALCULATE: COUNT, P95(duration_ms), AVG(message.size)                 │
└───────────────────────────────────────────────────────────────────────┘

Results:

╔══════════╦════════╦═══════╦══════════╦═══════════╗
║ Protocol ║ Status ║ Count ║ P95 (ms) ║ Avg Size  ║
╠══════════╬════════╬═══════╬══════════╬═══════════╣
║ slim     ║ OK     ║ 1,234 ║ 125      ║ 1,856     ║
║ slim     ║ ERROR  ║ 3     ║ 5,200    ║ 2,103     ║
║ a2a      ║ OK     ║ 856   ║ 45       ║ 512       ║
║ a2a      ║ TIMEOUT║ 1     ║ 30,050   ║ 1,024     ║
╚══════════╩════════╩═══════╩══════════╩═══════════╝

Heatmap (Duration by Protocol):

        slim                    a2a
 200ms  ████████░░░░░░░░        ░░░░░░░░░░░░░░░░
 150ms  ████████████░░░░        ░░░░░░░░░░░░░░░░
 100ms  ████████████████        ████░░░░░░░░░░░░
  50ms  ████████████████        ████████████████
   0ms  ████████████████        ████████████████
        ^                       ^
        Dense around 125ms      Dense around 45ms
"""


# ==============================================================================
# ERROR CASE EXAMPLE
# ==============================================================================


async def example_with_error():
    """Example showing what an error looks like in traces."""
    try:
        # This will timeout
        await slim_client.request_reply(
            session, message, remote_name="unavailable-agent", timeout=30
        )
    except TimeoutError:
        pass


"""
Langfuse Error View:

📊 Trace: example_with_error
   Duration: 30.05s
   Status: ❌ Error
   
   └─ 🔴 gen_ai.agent.communication                    [30050ms]
      Status: ERROR
      Error: TimeoutError
      
      Attributes:
      • protocol: slim
      • operation: request_reply
      • receiver.id: unavailable-agent
      • status_code: TIMEOUT                           ⚠️
      • error.type: TimeoutError                       ⚠️
      • error.message: Connection timeout after 30s    ⚠️
      
      Events:
      ⬤ message.sent (0ms)
      ⬤ message.timeout (30000ms)                      ⚠️
      ⬤ message.error (30050ms)                        ⚠️
         error.type: TimeoutError
         error.message: Connection timeout after 30s


Grafana Alert Query:

rate(traces{
  span.name="gen_ai.agent.communication",
  gen_ai.agent.communication.status_code=~"ERROR|TIMEOUT"
}[5m]) > 0.05

Alert: "Agent communication error rate > 5%"
"""


# ==============================================================================
# DASHBOARD EXAMPLES
# ==============================================================================

"""
Grafana Dashboard Panels:

┌─ Panel 1: Message Rate by Protocol ────────────────────────────┐
│                                                                 │
│  msgs/sec                                                       │
│    100 ┤                                                        │
│     75 ┤     ██  ██                                             │
│     50 ┤ ██  ██████  ██      ▓▓  ▓▓                             │
│     25 ┤ ████████████████    ▓▓▓▓▓▓▓▓    ░░  ░░                 │
│      0 └────────────────────────────────────────────────────    │
│         10:00      10:15      10:30      10:45                  │
│                                                                 │
│  Legend: ██ SLIM  ▓▓ A2A  ░░ MCP                                │
└─────────────────────────────────────────────────────────────────┘

┌─ Panel 2: P95 Latency by Operation ────────────────────────────┐
│                                                                 │
│  Latency (ms)                                                   │
│   Operation           P50    P95    P99    Max                  │
│   ─────────────────────────────────────────────                 │
│   publish             85     125    180    450                  │
│   request_reply       120    250    890    5200                 │
│   send_message        25     45     75     120                  │
│   broadcast_message   30     55     95     180                  │
│                                                                 │
│  🔴 Alert: request_reply P95 > 200ms threshold                  │
└─────────────────────────────────────────────────────────────────┘

┌─ Panel 3: Agent Network Graph ─────────────────────────────────┐
│                                                                 │
│      planning-agent                                             │
│           │                                                     │
│           │ SLIM: 1.2k msgs, 125ms avg                          │
│           ↓                                                     │
│      execution-agent                                            │
│           │                                                     │
│           ├─→ A2A: 850 msgs, 45ms avg → memory-agent           │
│           │                                                     │
│           └─→ A2A: 350 msgs, 52ms avg → analytics-agent        │
│                                                                 │
│  Node Size: Message volume                                      │
│  Edge Color: Latency (green=fast, red=slow)                     │
└─────────────────────────────────────────────────────────────────┘

┌─ Panel 4: Error Rate ──────────────────────────────────────────┐
│                                                                 │
│  Error %                                                        │
│     5% ┤                                                        │
│     4% ┤                                                        │
│     3% ┤                                 ⚠️                      │
│     2% ┤         ⚠️                      ███                     │
│     1% ┤   ██    ███                     ███                    │
│     0% └────────────────────────────────────────────────────    │
│         10:00   10:15   10:30   10:45                           │
│                                                                 │
│  Breakdown by Error Type:                                       │
│  • TimeoutError: 67%                                            │
│  • ConnectionError: 25%                                         │
│  • Other: 8%                                                    │
└─────────────────────────────────────────────────────────────────┘
"""


if __name__ == "__main__":
    # Initialize observability
    Observe.init(
        app_name="multi-agent-system",
        api_endpoint="https://api.observe.agntcy.org",
        api_key="your-api-key",
    )

    # Run the example
    asyncio.run(example_multi_agent_workflow())

    # Traces are automatically exported to your configured backend
    # (Langfuse, Grafana, Jaeger, etc.)
