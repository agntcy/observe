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
8. [What's the difference between `@graph` and `@agent` decorators?](#whats-the-difference-between-graph-and-agent-decorators)

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
@graph(name="graph_name"): Instruments LangGraph state graphs for observability
@agent(name="agent_name"): Tracks individual agent nodes and activities
@tool(name="tool_name"): Monitors tool usage and performance
@process_slim_msg("agent_name"): Instruments SLIM message processing for inter-agent communication
```
Session Management - Critical Entry Point

⚠️ IMPORTANT: session_start() MUST be called at the entry point of execution in Multi-Agentic Systems.

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

## LangGraph Integration

LangGraph is a framework for building stateful, multi-agent applications. Here's how to use it with Observe SDK:

### Decorating Agents

Use the `@agent` decorator for individual agents:

```python
from ioa_observe.sdk.decorators import agent
from langchain_core.messages import HumanMessage

@agent(name="processing_agent")
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
```


**NOTE:** Using the `@graph` annotation is necessary to track the multi-agent interaction topology.

### Decorating Tools

Use the `@tool` decorator to track tool operations:

```python
from ioa_observe.sdk.decorators import tool

@tool(name="Python REPL tool")
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

@tool(name="multiply")
def multiply(a: float, b: float) -> float:
    """Multiply two numbers and returns the product"""
    return a * b

@tool(name="add")
def add(a: float, b: float) -> float:
    """Add two numbers and returns the sum"""
    return a + b
```

### Class-Based Agents

Decorate agent classes:

```python
from ioa_observe.sdk.decorators import agent
from llama_index.core.agent.workflow import FunctionAgent

@agent(name="AgentOne")
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
@agent(name="multi_agent_workflow")
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

@agent(name="ChatbotAgent")
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


## SLIM-Based Multi-Agent Systems

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
@graph(name="multi_agent_workflow_graph")  # Captures workflow structure
@agent(name="multi_agent_workflow")                  # Tracks this as an agent entity
class MultiAgentWorkflow(AgentWorkflow):
    """This class IS an agent but MANAGES a graph"""
```


### Summary
You need both decorators when:

1. The entity is simultaneously an `agent` AND track the workflow graph
2. You want both agent-level metrics AND graph-level topology analysis
3. You need to track individual agent behavior within the context of a larger multi-agent system

4. The decorators complement each other - @agent focuses on the behavioral aspects while @graph focuses on the structural aspects of multi-agent systems.
