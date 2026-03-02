# Getting Started with IOA Observe SDK for Agent Frameworks

This guide illustrates how to integrate IOA Observe SDK with popular agent frameworks including LangGraph, CrewAI, LlamaIndex among others.
The Observe SDK provides comprehensive observability through decorators to track agent activity, tool usage, workflow execution, and inter-agent communication.

## Table of Contents
1. [Prerequisites](#prerequisites)
2. [Core SDK Components](#core-sdk-components)
   - [Key Decorators](#key-decorators)
   - [Session Management - Critical Entry Point](#session-management---critical-entry-point)
3. [LangGraph Integration](#langgraph-integration)
   - [Decorating Agents](#decorating-agents)
   - [Decorating Graphs](#decorating-graphs)
   - [Decorating Tools](#decorating-tools)
4. [LlamaIndex Integration](#llamaindex-integration)
   - [Function-Based Tools](#function-based-tools)
   - [Class-Based Agents](#class-based-agents)
   - [Workflow Graphs](#workflow-graphs)
5. [Multi-Agent Supervisor Pattern with Observe SDK](#multi-agent-supervisor-pattern-with-observe-sdk)
6. [Best Practices](#best-practices)
7. [SLIM-Based Multi-Agentic Systems](#slim-based-multi-agentic-systems)
   - [Initializing the SLIM Connector with your agent](#initializing-the-slim-connector-with-your-agent)
   - [Receiving Messages with a Callback](#receiving-messages-with-a-callback)
   - [Starting the Message Receiver](#starting-the-message-receiver)
   - [Publishing Messages](#publishing-messages)
8. [A2A Protocol support](#a2a-protocol-support)
   - [Initializing the A2A Instrumentor (Agent-to-Agent Communication)](#initializing-the-a2a-instrumentor)
9. [MCP Protocol support](#mcp-protocol-support)
10. [Agent Handoff Tracking via Span Links](#agent-handoff-tracking-via-span-links)
    - [How It Works](#how-it-works)
    - [Cross-Process Agent Linking](#cross-process-agent-linking)
11. [Parallel Agent & Tool Invocation (Fork/Join)](#parallel-agent--tool-invocation-forkjoin)
    - [Parallel Agents (Fan-Out / Fan-In)](#parallel-agents-fan-out--fan-in)
    - [Parallel Tools (Fan-Out Within a Single Agent)](#parallel-tools-fan-out-within-a-single-agent)
    - [Fork/Join Span Attributes Reference](#forkjoin-span-attributes-reference)
    - [Fork/Join Events](#forkjoin-events)
12. [Automatic Session End Detection](#automatic-session-end-detection)
13. [What's the difference between `@graph` and `@agent` decorators?](#whats-the-difference-between-graph-and-agent-decorators)

## Prerequisites

Before getting started:

1. Install the IOA Observe SDK
2. Set up your environment variables:
   ```
   OTLP_HTTP_ENDPOINT=http://localhost:4318
   ```
3. Initialize Observe with your service name and Otel collector endpoint:
   ```python
   from ioa_observe.sdk import Observe
   import os
   Observe.init("your_service_name", api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))
   ```

## Core SDK Components
### Key Decorators
```
@graph(name="graph_name"): Captures MAS topology state for observability
@agent(name="agent_name", description="Some description"): Tracks individual agent nodes and activities
@tool(name="tool_name", description="Some description"): Monitors tool usage and performance
@process_slim_msg("agent_name"): Instruments SLIM message processing for inter-agent communication
```

### Session Management - Critical Entry Point

⚠️ **IMPORTANT:** `session_start()` can be used directly or as a context manager to automatically manage the agent session lifecycle. This is especially useful for propagating session context (such as session IDs) across HTTP or other communication boundaries.

#### Using `session_start()` Directly

Simply use the `session_start()` function, if you use our connectors to manage the agent-agent communication, such as [SLIM](#slim-based-multi-agentic-systems) etc.

```python
from ioa_observe.sdk.tracing import session_start

def main():
    load_environment_variables()
    init_gateway_conn()

    graph = build_graph()

    # CRITICAL: Start observability session at entry point of your execution
    session_start()

    # Execute your multi-agent workflow
    inputs = {"messages": [HumanMessage(content="Write a story about a cat")]}
    result = graph.invoke(inputs)
```

#### Using `session_start()` as a Context Manager

You can also use `session_start()` in a `with` block to ensure the session is started and properly closed:

```python
from ioa_observe.sdk.tracing import session_start

def main():
    load_environment_variables()
    with session_start() as session_id:
        graph = build_graph()
        inputs = {
            "messages": [HumanMessage(content="Write a story about a cat")],
            "session_id": session_id,  # Pass session_id to downstream agents/tools
        }
        result = graph.invoke(inputs)
        print(result)
```

**Manual Session Propagation via HTTP (Agent-to-Agent Communication):**

To enable agent-to-agent communication, you can propagate the session ID by including it in the HTTP headers when sending a request to another agent's server endpoint. This ensures full traceability and observability across distributed agents.

Example:

```python
from ioa_observe.sdk.decorators import agent
from ioa_observe.sdk.tracing import session_start

from langchain_core.messages.utils import convert_to_openai_messages
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph.message import add_messages

import json
import os
import requests
from typing import Annotated, Any, Dict, List, TypedDict
import uuid

class GraphState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    session_id: dict

def send_http_request(payload: dict, session_id: dict) -> dict:
    """Send an HTTP request to another agent's server endpoint with session ID."""
    endpoint = os.getenv("REMOTE_SERVER_URL", "http://localhost:5000/runs")
    # Include the session ID and other metadata in the HTTP headers for downstream agent observability
    headers = session_id or {}
    headers["Content-Type"] = "application/json"

    try:
        response = requests.post(endpoint, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        # Handle error as needed
        return {"error": str(e)}

@agent(name="remote_agent_http", description="Remote agent that communicates via HTTP")
def node_remote_http(state: GraphState) -> Dict[str, Any]:
    if not state["messages"]:
        logger.error(json.dumps({"error": "GraphState contains no messages"}))
        return {"messages": [HumanMessage(content="Error: No messages in state")]}

    query = state["messages"][-1].content
    logger.info(json.dumps({"event": "sending_request", "query": query}))

    messages = convert_to_openai_messages(state["messages"])

    payload = {
        "agent_id": "remote_agent",
        "input": {"messages": messages},
        "model": "gpt-4o",
        "metadata": {"id": str(uuid.uuid4())},
        "route": "/runs",
    }

    headers = state["session_id"]

    response_data = send_http_request(payload, headers)
    print(f"Response from server: {response_data}")
    return {"messages": [HumanMessage(content=json.dumps(response_data))]}
```

Then in your other agent, you can retrieve the session ID from the headers:

```python

from ioa_observe.sdk.tracing.context_utils import set_context_from_headers

# Flask app
app = Flask(__name__)
logger = logging.getLogger("app")

@app.route("/runs", methods=["POST"])
def process_message():
    """
    Process incoming messages from agents and set context from headers.
    """
    try:
        payload = request.get_json(force=True)
        # get headers
        headers = request.headers
        print("Headers:", headers)
        if headers:
            set_context_from_headers(headers)
        logger.debug("Received payload: %s", payload)
        # Process the payload as needed
    except Exception as e:
        logger.error("Error processing message: %s", str(e))
        return jsonify({"error": str(e)}), 500

```

## LangGraph Integration

LangGraph is a framework for building stateful, multi-agent applications. Here's how to use it with Observe SDK:

### Decorating Agents

Use the `@agent` decorator for individual agents:

```python
from ioa_observe.sdk.decorators import agent
from langchain_core.messages import HumanMessage

@agent(name="processing_agent", description="Agent that processes user input")
def processing_node(state: GraphState) -> Dict[str, Any]:
    """Process user input and generate response."""
    # Agent processing logic
    prompt = ChatPromptTemplate([...])
    llm = ChatOpenAI(model="gpt-4o")
    response = (prompt | llm).invoke({"messages": state["messages"]})
    return {"messages": [response]}
```

### Decorating Graphs

Use the `@graph` decorator for the entire agent graph:

```python
from ioa_observe.sdk.decorators import graph
from langgraph.graph import StateGraph, START, END

@graph(name="multi_agent_graph")
def build_graph():
    """Constructs the state graph for handling requests."""
    builder = StateGraph(GraphState)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("researcher", research_node)
    builder.add_node("coder", code_node)
    builder.add_edge(START, "supervisor")
    builder.add_edge("supervisor", "researcher")
    builder.add_edge("researcher", "coder")
    builder.add_edge("coder", END)
    return builder.compile()
```


**NOTE:** Using the `@graph` annotation is necessary to track the multi-agent interaction topology.

### Decorating Tools

Use the `@tool` decorator to track tool operations:

```python
from ioa_observe.sdk.decorators import tool

@tool(name="Python REPL tool", description="Execute Python code in a REPL environment")
def python_repl_tool(code: Annotated[str, "The python code to execute"]):
    """Execute Python code and do math."""
    try:
        result = repl.run(code)
    except BaseException as e:
        return f"Failed to execute. Error: {repr(e)}"
    return f"Successfully executed:\n```python\n{code}\n```\nStdout: {result}"
```


## LlamaIndex Integration

LlamaIndex supports two patterns: function-based and class-based agents.

### Function-Based Tools

Decorate individual tool functions:

```python
from ioa_observe.sdk.decorators import tool

@tool(name="multiply", description="Multiply two numbers")
def multiply(a: float, b: float) -> float:
    """Multiply two numbers and returns the product"""
    return a * b

@tool(name="add", description="Add two numbers")
def add(a: float, b: float) -> float:
    """Add two numbers and returns the sum"""
    return a + b
```

### Class-Based Agents

Decorate agent classes:

```python
from ioa_observe.sdk.decorators import agent
from llama_index.core.agent.workflow import FunctionAgent

@agent(name="AgentOne", description="Agent for basic data processing")
class AgentOne(FunctionAgent):
    """Agent One class for initial processing tasks."""

    def __init__(self, llm):
        super().__init__(
            name="AgentOne",
            tools=[tool_one],
            llm=llm,
            system_prompt="You are agent one.",
            can_handoff_to=["AgentTwo"]
        )
        self.description = "This is Agent One, responsible for initial processing."
```

### Workflow Graphs

Decorate workflow classes with both `@graph` and `@agent`:

```python
from ioa_observe.sdk.decorators import graph, agent
from llama_index.core.agent.workflow import AgentWorkflow

@graph(name="multi_agent_workflow")
@agent(name="multi_agent_workflow", description="Workflow managing multiple agents")
class MultiAgentWorkflow(AgentWorkflow):
    """Custom workflow class that manages multiple agents."""

    def __init__(self):
        super().__init__(
            agents=[agent_one, agent_two],
            root_agent="AgentOne"
        )

    async def run(self, user_msg: str):
        return await super().run(user_msg)
```


These decorators will automatically record agent activity and heartbeats for monitoring agent availability metrics.

## Multi-Agent Supervisor Pattern with Observe SDK

This example demonstrates how to implement and monitor a supervisor pattern where one agent orchestrates and coordinates multiple specialized agents.
Implementing the Supervisor Pattern

Step 1: Initialize Observe

```python
from ioa_observe.sdk import Observe
import os
Observe.init("moderator-agent", api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))
```

Step 2: Create Individual Specialized Agents
Decorate each agent with the `@agent` decorator to track its activities:

```python
from ioa_observe.sdk.decorators import agent

@agent(name="ChatbotAgent", description="Agent that handles chatbot interactions")
class ChatbotAgent:
    def __init__(self):
        # Agent initialization code...
        self.chain = PROMPT_TEMPLATE | llm | parser

    def invoke(self, *args, **kwargs):
        return self.chain.invoke(*args, **kwargs)
```
Step 3: Create Supervisor agent class to manage the specialized agents. This class is dummy and does not implement any specific logic, but it serves as a coordinator for the specialized agents.

The `get_agents` method initializes and returns the specialized agents in a list, which can be used to infer the topology of the multi-agent system.
1. A simple topology is all agents are connected to the moderator agent, a fairly common pattern in multi-agent systems. The moderator agent can then manage the interactions between these agents.
2. Or if 'moderator' doesn't exist, we assume all agents are connected to every other agent, which is a fully connected topology.

```python
from ioa_observe.sdk.decorators import graph

class SupervisorAgent:
    def __init__(self):
        self.evaluator_agent, self.moderator_agent = None, None
        # Communication setup code...

    @graph(name="moderator_evaluator_workflow")
    def get_agents(self):
        return ["evaluator", "web_surfer", "moderator"]
```
## Best Practices

- **Initialize Early**: Call `Observe.init()` before any other SDK operations
- **Meaningful Names**: Use descriptive names for decorators to aid in observability dashboards
- **Session Management**: Always call `session_start()` at the main entry point, not within individual agents
- **SLIM Setup**: Initialize SLIM components before starting inter-agent communication
- **Environment Setup**: Ensure all required environment variables are properly configured

### Agent State Management and Return Attributes

When implementing agents with state management (e.g., in LangGraph), your agent functions should return dictionaries that update the state. While the SDK primarily focuses on observability instrumentation, you may need to include additional attributes in your return values for workflow orchestration:

```python
@agent(name="triage_agent", description="Triages incoming requests")
def triage_agent(state: GraphState) -> Dict[str, Any]:
    """Triage and route requests to appropriate service agents."""
    # Process the request
    result = process_triage(state)

    # Return state updates including workflow control attributes
    return {
        # Standard state updates
        "messages": [HumanMessage(content=result["response"])],

        # Workflow control attributes (framework-specific)
        "goto": "service_agent",           # Next node routing
        "success": True,                    # Execution status
        "task_id": result["id"],            # Task tracking
        "context_id": result["contextId"],  # Context correlation
        "action": "triage_completed"        # Workflow action
    }
```

**Important Notes:**
- Attributes like `goto`, `success`, `task_id`, `context_id`, and `action` are **workflow control attributes** for your application logic, not SDK-specific requirements
- The SDK automatically captures observability data through decorators—you don't need to add special attributes for telemetry
- Structure your return values based on your framework's requirements (LangGraph, LlamaIndex, etc.)
- The SDK will instrument the function execution regardless of what you return


## SLIM Based Multi-Agentic Systems

SLIM (Secure Low-Latency Interactive Messaging) enables communication between AI agents, supporting patterns like request-response, publish-subscribe, fire-and-forget, and streaming. Built on gRPC, SLIM provides secure and scalable agent interactions.

Repository: https://github.com/agntcy/slim

For distributed agent systems using the SLIM protocol, the Observe SDK offers additional instrumentation:


### Initializing the SLIM Connector with your agent

```python
from ioa_observe.sdk.connectors.slim import SLIMConnector, process_slim_msg
from ioa_observe.sdk.instrumentations.slim import SLIMInstrumentor

# Initialize SLIM connector
slim_connector = SLIMConnector(
    remote_org="cisco",
    remote_namespace="default",
    shared_space="chat",
)

# Register agents with the connector
slim_connector.register("remote_client_agent")

# Instrument SLIM communications
SLIMInstrumentor().instrument()
```

### Receiving Messages with a Callback

Add the decorator `process_slim_msg` to the callback function to process incoming messages. This function will be called whenever a message is received in the shared space.

```python

# Define a callback to process incoming messages
from ioa_observe.sdk.connectors.slim import SLIMConnector, process_slim_msg
import json
from typing import Dict, Any

@process_slim_msg("remote_client_agent")
async def send_and_recv(msg) -> Dict[str, Any]:
    """Send message to remote endpoint and wait for reply."""
    gateway = GatewayHolder.gateway
    session_info = GatewayHolder.session_info

    if gateway is not None:
        await gateway.publish(session_info, msg.encode(), "cisco", "default", "server")
        async with gateway:
            _, recv = await gateway.receive(session=session_info.id)
    else:
        raise RuntimeError("Gateway is not initialized yet!")

    response_data = json.loads(recv.decode("utf8"))
    return {"messages": response_data.get("messages", [])}
```

### Starting the Message Receiver

```python
# Start receiving messages from the SLIM shared space
await slim.receive(callback=on_message_received)
```

### Publishing Messages

```python
# Publish a message to the SLIM shared space
message = {"type": "ChatMessage", "author": "moderator", "message": "Hello, world!"}
await slim.publish(msg=json.dumps(message).encode("utf-8"))
```

We will observe various events and metrics being sent to the Otel collector as we interact with other agents in the shared space via SLIM.


## A2A Protocol support

The Agent-to-Agent (A2A) Protocol, introduced by Google, is a cross-platform specification for enabling AI agents to communicate, collaborate, and delegate tasks across heterogeneous systems.
The IOA Observe SDK provides built-in support for A2A protocol, allowing you to instrument your agents for A2A communication.

### Initializing the A2A Instrumentor
To enable agent-to-agent communication, you can use the `A2AInstrumentor` to instrument your agents for A2A protocol support.

```python
from ioa_observe.sdk.instrumentations.a2a import A2AInstrumentor
# Initialize the A2A instrumentor
A2AInstrumentor().instrument()
```


## MCP Protocol support

ModelContextProtocol (MCP) is an open protocol designed to standardize how large language models (LLMs) and other AI agents interact with external tools, memory, and context.

Observe SDK provides built-in support for MCP, allowing you to instrument your servers/clients for MCP.

### Initializing the MCP Instrumentor
```python
from ioa_observe.sdk.instrumentations.mcp import McpInstrumentor
# Initialize the MCP instrumentor
McpInstrumentor().instrument()
```
## Agent Handoff Tracking via Span Links

The SDK automatically tracks **agent handoffs** using OpenTelemetry Span Links. When one agent invokes another within the same session, the SDK creates a link from the new agent's span back to the previous agent's span, enabling full traceability of the agent execution chain.

### How It Works

- Each agent span is automatically annotated with:
  - `ioa_observe.agent.sequence` — the agent's position in the execution chain
  - `ioa_observe.agent.previous` — the name of the previously executing agent
  - A **span link** with `link.type = "agent_handoff"` pointing to the previous agent's span

No code changes are needed — the `@agent` decorator handles this automatically as long as a session is active:

```python
from ioa_observe.sdk.decorators import agent
from ioa_observe.sdk.tracing import session_start

@agent(name="planner", description="Plans tasks")
async def planner(state: dict) -> dict:
    return {"plan": "Step 1, Step 2, Step 3"}

@agent(name="executor", description="Executes tasks")
async def executor(state: dict) -> dict:
    # This span will have a link back to the planner's span
    return {"result": "done"}

async def main():
    session_start()
    plan = await planner({"query": "build something"})
    result = await executor(plan)
```

### Cross-Process Agent Linking

For distributed multi-agent systems where agents communicate over HTTP, SLIM, or A2A, the SDK propagates agent linking context in message headers via `get_current_context_headers()`. The receiving agent uses `set_context_from_headers()` to restore context, and span links are created automatically:

```python
from ioa_observe.sdk.tracing.context_utils import (
    get_current_context_headers,
    set_context_from_headers,
)

# Sender side: headers now include agent linking info (span_id, trace_id, agent_name, sequence)
headers = get_current_context_headers()
# ... send headers with your HTTP/SLIM/A2A request ...

# Receiver side: restores trace context + agent linking
set_context_from_headers(headers)
```

The headers automatically include `last_agent_span_id`, `last_agent_trace_id`, `last_agent_name`, and `agent_sequence` so that the receiving agent can create proper span links.

---

## Parallel Agent & Tool Invocation (Fork/Join)

The SDK provides **implicit fork/join detection** for fan-out / fan-in patterns. When multiple agents or tools execute in parallel (e.g., via `asyncio.gather`), the SDK automatically detects this and annotates spans with fork/join metadata — no manual annotation required.

### Parallel Agents (Fan-Out / Fan-In)

When a coordinator agent dispatches multiple agents in parallel, the SDK detects that they share the same parent agent and marks them as **fork branches**:

```python
from ioa_observe.sdk.decorators import agent, tool
from ioa_observe.sdk.tracing import session_start
import asyncio

@agent(name="researcher", description="Performs web research")
async def researcher(state: dict) -> dict:
    return {"research_results": "...", "agent": "researcher"}

@agent(name="coder", description="Writes and runs code")
async def coder(state: dict) -> dict:
    return {"code_output": "...", "agent": "coder"}

@agent(name="analyst", description="Analyzes data")
async def analyst(state: dict) -> dict:
    return {"analysis": "...", "agent": "analyst"}

@agent(name="coordinator", description="Dispatches work to specialist agents")
async def coordinator(state: dict) -> dict:
    # These three agents run in parallel — the SDK detects the fork automatically
    results = await asyncio.gather(
        researcher(state),
        coder(state),
        analyst(state),
    )
    return {"branch_results": list(results)}

@agent(name="aggregator", description="Collects and summarizes parallel results")
async def aggregator(state: dict) -> dict:
    # This agent runs after all branches complete — the SDK detects the join
    return {"summary": "aggregated"}

async def main():
    session_start()
    coord_result = await coordinator({"query": "analyze AI trends"})
    final = await aggregator(coord_result)
```

**What the SDK emits:**

| Span | Attributes |
|------|------------|
| `coordinator.agent` | Parent agent |
| `researcher.agent` | `ioa_observe.fork.id`, `ioa_observe.fork.branch_index=0` |
| `coder.agent` | `ioa_observe.fork.id`, `ioa_observe.fork.branch_index=1` |
| `analyst.agent` | `ioa_observe.fork.id`, `ioa_observe.fork.branch_index=2` |
| `aggregator.agent` | `ioa_observe.join.fork_id`, `ioa_observe.join.branch_count=3`, span links to all branches |

### Parallel Tools (Fan-Out Within a Single Agent)

The SDK also detects when a single agent calls multiple tools in parallel:

```python
@tool(name="web_search")
async def web_search(query: str) -> str:
    return f"results for '{query}'"

@tool(name="run_code")
async def run_code(code: str) -> str:
    return f"output of '{code}'"

@tool(name="analyze_data")
async def analyze_data(dataset: str) -> str:
    return f"analysis of '{dataset}'"

@agent(name="multi_tool_agent", description="Calls multiple tools in parallel")
async def multi_tool_agent(state: dict) -> dict:
    # Parallel tool calls — fork detected automatically
    search, code, analysis = await asyncio.gather(
        web_search("AI agents"),
        run_code("fibonacci(10)"),
        analyze_data("quarterly_sales"),
    )
    return {"search": search, "code": code, "analysis": analysis}
```

Each parallel tool span is annotated with `ioa_observe.fork.id` and `ioa_observe.fork.branch_index`, scoped to the parent agent.

### Fork/Join Span Attributes Reference

| Attribute | Set On | Description |
|-----------|--------|-------------|
| `ioa_observe.fork.id` | Branch spans | Unique ID for the fork group |
| `ioa_observe.fork.branch_index` | Branch spans | 0-based index of this branch |
| `ioa_observe.fork.parent_sequence` | Branch spans | Sequence number of the dispatching agent |
| `ioa_observe.fork.parent_name` | Branch spans | Name of the dispatching agent |
| `ioa_observe.join.fork_id` | Join span | Fork ID being joined |
| `ioa_observe.join.branch_count` | Join span | Number of branches that were joined |

### Fork/Join Events

| Event | Description |
|-------|-------------|
| `agent.join` | Emitted on the joining span with `ioa_observe.join.fork_id` and `ioa_observe.join.branch_count` |

---

## Automatic Session End Detection

The SDK automatically detects when a session has become idle and emits a `session.end` span. This is useful for tracking session lifecycles without requiring explicit teardown logic.

### How It Works

- A background watcher thread monitors session activity every 30 seconds
- When a session has been idle for **5 minutes** (no new spans emitted), the SDK emits a `session.end` span
- The `session.end` span is placed under the same trace as the session's last activity
- At process exit, any remaining active sessions also receive a `session.end` span
- Duplicate `session.end` spans are prevented automatically

### Session End Span Attributes

| Attribute | Description |
|-----------|-------------|
| `session.id` | The session that ended |
| `session.ended_at` | Timestamp of the last activity before the session went idle |
| `observe.workflow.name` | The workflow name associated with the session |

No code changes are needed — session end detection is fully automatic once you call `session_start()`.

---

## What's the difference between `@graph` and `@agent` decorators?

### Different Observability Purposes

#### @agent Decorator
- **Purpose**: Tracks individual agent activities and behaviors
- **Focuses on**: Agent-level metrics like availability, recovery, heartbeats, and execution success/failure
- **Span Kind**: `ObserveSpanKindValues.AGENT`
- **Records**: Agent start/end events, agent interpretation scores, connection reliability

#### @graph Decorator
- **Purpose**: Captures the overall workflow topology structure and multi-agent interactions
- **Focuses on**: Graph topology, dynamism, determinism scores, and inter-agent relationships
- **Span Kind**: "graph" (special case)
- **Records**: Graph structure as JSON, topology analysis, workflow-level metrics

### Why Both Are Needed

When you have a class that represents both an individual agent and manages a complete workflow graph, you need both decorators to capture different aspects:

```python
@graph(name="multi_agent_workflow_graph")                 # Captures workflow structure
@agent(name="multi_agent_workflow", description="Multi agent workflow")       # Tracks this as an agent entity
class MultiAgentWorkflow(AgentWorkflow):
    """This class IS an agent but MANAGES a graph"""
```


### Summary
You need both decorators when:

1. The entity is simultaneously an `agent` AND track the workflow graph
2. You want both agent-level metrics AND graph-level topology analysis
3. You need to track individual agent behavior within the context of a larger multi-agent system

4. The decorators complement each other - @agent focuses on the behavioral aspects while @graph focuses on the structural aspects of multi-agent systems.
