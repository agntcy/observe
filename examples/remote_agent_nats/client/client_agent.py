# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

# Description: This file contains a sample graph client that makes a stateless request to the Remote Graph Server.
# Usage: uv run python client/client_agent.py

import asyncio
import json
import logging
import os
import uuid
from typing import Annotated, Any, Dict, List

from typing_extensions import TypedDict

import nats
from dotenv import find_dotenv, load_dotenv
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.messages.utils import convert_to_openai_messages
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from ioa_observe.sdk import Observe
from ioa_observe.sdk.tracing import session_start

from ioa_observe.sdk.instrumentations.nats import NATSInstrumentor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("client_agent")

serviceName = "remote-client-agent"
Observe.init(serviceName, api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT", "http://localhost:4318"))

LOCAL_AGENT = "client"
REMOTE_AGENT = "server"

class GatewayHolder:
    nc = None

def load_environment_variables(env_file: str | None = None) -> None:
    """
    Load environment variables from a .env file safely.

    This function loads environment variables from a `.env` file, ensuring
    that critical configurations are set before the application starts.

    Args:
        env_file (str | None): Path to a specific `.env` file. If None,
                               it searches for a `.env` file automatically.

    Behavior:
    - If `env_file` is provided, it loads the specified file.
    - If `env_file` is not provided, it attempts to locate a `.env` file in the project directory.
    - Logs a warning if no `.env` file is found.

    Returns:
        None
    """
    env_path = env_file or find_dotenv()

    if env_path:
        load_dotenv(env_path, override=True)
        logger.info(f".env file loaded from {env_path}")
    else:
        logger.warning("No .env file found. Ensure environment variables are set.")


def decode_response(response_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Decodes the JSON response from the remote server and extracts relevant information.

    Args:
        response_data (Dict[str, Any]): The JSON response from the server.

    Returns:
        Dict[str, Any]: A structured dictionary containing extracted response fields.
    """
    try:
        agent_id = response_data.get("agent_id", "Unknown")
        output = response_data.get("output", {})
        model = response_data.get("model", "Unknown")
        metadata = response_data.get("metadata", {})

        # Extract messages if present
        messages = output.get("messages", [])

        return {
            "agent_id": agent_id,
            "messages": messages,
            "model": model,
            "metadata": metadata,
        }
    except Exception as e:
        return {"error": f"Failed to decode response: {str(e)}"}


# Define the graph state
class GraphState(TypedDict):
    """Represents the state of the graph, containing a list of messages."""

    messages: Annotated[List[BaseMessage], add_messages]
    gateway: GatewayHolder


# @measure_chain_completion_time
async def send_and_recv(msg) -> Dict[str, Any]:
    """
    Send a message to the remote endpoint and
    waits for the reply using pubsub pattern
    """
    if GatewayHolder.nc is None:
        raise RuntimeError("NATS connection is not initialized yet")
    
    nc = GatewayHolder.nc
    try:
        reply = await nc.request(REMOTE_AGENT, msg.encode(), timeout=30)
    except Exception as e:
        logger.error(f"Error in NATS request-reply communication: {e}")
        raise e

    response_data = json.loads(reply.data.decode("utf8"))

    # check for errors
    error_code = response_data.get("error")
    if error_code is not None:
        error_msg = {
            "error": "NATS request failed",
            "status_code": error_code,
            "exception": response_data.get("message"),
        }
        logger.error(json.dumps(error_msg))
        return {"messages": [HumanMessage(content=json.dumps(error_msg))]}

    else:
        # decode message
        decoded_response = decode_response(response_data)
        logger.info(decoded_response)

        return {"messages": decoded_response.get("messages", [])}


# @agent(name="remote_client_agent")
async def node_remote_nats(state: GraphState) -> Dict[str, Any]:
    if not state["messages"]:
        logger.error(json.dumps({"error": "GraphState contains no messages"}))
        return {"messages": [HumanMessage(content="Error: No messages in state")]}

    # Extract the latest user query
    query = state["messages"][-1].content
    logger.info(json.dumps({"event": "sending_request", "query": query}))

    messages = convert_to_openai_messages(state["messages"])

    # payload to send to autogen server at /runs endpoint
    payload = {
        "agent_id": "remote_agent",
        "input": {"messages": messages},
        "model": "gpt-4o",
        "metadata": {"id": str(uuid.uuid4())},
        # Add the route field to emulate the REST API
        "route": "/runs",
    }

    msg = json.dumps(payload)
    res = await send_and_recv(msg)
    return res

# Build the state graph
# @graph_decorator(name="remote_client_agent_graph")
async def build_graph() -> Any:
    """
    Constructs the state graph for handling requests.

    Returns:
        StateGraph: A compiled LangGraph state graph.
    """
    builder = StateGraph(GraphState)
    builder.add_node("node_remote_request_stateless", node_remote_nats)
    builder.add_edge(START, "node_remote_request_stateless")
    builder.add_edge("node_remote_request_stateless", END)
    return builder.compile()

# @log_connection_events
# @measure_connection_latency
async def init_nats_conn():
    """
    Initialize the NATS connection
    """
    port = os.getenv("PORT", "4222")
    address = os.getenv("NATS_SERVER_ADDR", "127.0.0.1")
    uri = f"nats://{address}:{port}"

    # Connect to NATS server
    nc = await nats.connect(uri)

    logger.info(f"Connected to NATS server at {address}")

    # Instrument NATS communication
    NATSInstrumentor().instrument()

    GatewayHolder.nc = nc

async def main():
    load_environment_variables()
    await init_nats_conn()

    session_start()  # entry point in execution

    graph = await build_graph()

    inputs = {"messages": [HumanMessage(content="Write a very short story about a cat")]}
    logger.info({"event": "invoking_graph", "inputs": inputs})
    result = await graph.ainvoke(inputs)
    logger.info({"event": "final_result", "result": result})


# Main execution
if __name__ == "__main__":
    asyncio.run(main())