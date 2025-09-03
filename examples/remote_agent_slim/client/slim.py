# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

# Description: This file contains a sample graph client that makes a stateless request to the Remote Graph Server.
# Usage: python3 client/slim.py

import asyncio
import datetime
import json
import os
import uuid
from typing import Annotated, Any, Dict, List

from typing_extensions import TypedDict

import slim_bindings
from dotenv import find_dotenv, load_dotenv
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.messages.utils import convert_to_openai_messages
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from ioa_observe.sdk import Observe
from ioa_observe.sdk.tracing import session_start

from ioa_observe.sdk.instrumentations.slim import SLIMInstrumentor

from logging_config import configure_logging

logger = configure_logging()

serviceName = "remote-client-agent"
Observe.init(serviceName, api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))

ORGANIZATION = "cisco"
NAMESPACE = "default"
LOCAL_AGENT = "client"
REMOTE_AGENT = "server"


class GatewayHolder:
    gateway = None
    session_info = None


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


# Helper function to create shared secret identity
def shared_secret_identity(identity, secret):
    """
    Create a provider and verifier using a shared secret.
    """
    provider = slim_bindings.PyIdentityProvider.SharedSecret(
        identity=identity, shared_secret=secret
    )
    verifier = slim_bindings.PyIdentityVerifier.SharedSecret(
        identity=identity, shared_secret=secret
    )
    return provider, verifier


# Helper function to split ID into components
def split_id(id_str):
    """Split an ID into its components"""
    try:
        organization, namespace, app = id_str.split("/")
    except ValueError as e:
        print("Error: IDs must be in the format organization/namespace/app.")
        raise e
    return slim_bindings.PyName(organization, namespace, app)


# @measure_chain_completion_time
# @process_slim_msg("remote_client_agent")
async def send_and_recv(msg) -> Dict[str, Any]:
    """
    Send a message to the remote endpoint and
    waits for the reply using pubsub pattern
    """

    gateway = GatewayHolder.gateway
    session_info = GatewayHolder.session_info
    if gateway is not None and session_info is not None:
        remote_name = split_id(f"{ORGANIZATION}/{NAMESPACE}/{REMOTE_AGENT}")
        try:
            remote_name = split_id(f"{ORGANIZATION}/{NAMESPACE}/{REMOTE_AGENT}")
            _, recv = await gateway.request_reply(
                session_info,
                msg.encode(),
                remote_name,
                timeout=datetime.timedelta(seconds=30),
            )

        except Exception as e:
            logger.error(f"Error in request-reply communication: {e}")
            # raise e
    else:
        raise RuntimeError("Gateway or session is not initialized yet!")
    session_info, _ = await gateway.receive()
    session_info, reply = await gateway.receive(session=session_info.id)
    print(f"Got reply: {reply.decode()}")
    response_data = json.loads(reply.decode("utf8"))

    # check for errors
    error_code = response_data.get("error")
    if error_code is not None:
        error_msg = {
            "error": "SLIM request failed",
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
def node_remote_slim(state: GraphState) -> Dict[str, Any]:
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
    res = asyncio.run(send_and_recv(msg))
    return res


# @log_connection_events
# @measure_connection_latency
async def connect_to_gateway(address):
    """
    Connect to the SLIM gateway using the new data-plane API
    """
    local_id = f"{ORGANIZATION}/{NAMESPACE}/{LOCAL_AGENT}"

    # Use shared secret for authentication (for demo purposes)
    shared_secret = os.getenv("SLIM_SHARED_SECRET", "demo-secret")

    # Initialize tracing
    # await slim_bindings.init_tracing(
    #     {"log_level": "info", "opentelemetry": {"enabled": True}}
    # )

    # Create identity provider and verifier
    provider, verifier = shared_secret_identity(local_id, shared_secret)

    # Split the local ID into components
    local_name = split_id(local_id)

    # Create SLIM client with new API
    gateway = await slim_bindings.Slim.new(local_name, provider, verifier)

    # Connect to the gateway server
    connection_config = {"endpoint": address, "tls": {"insecure": True}}
    _ = await gateway.connect(connection_config)

    # # Subscribe to receive replies
    await gateway.subscribe(local_name)

    # Set route to remote agent
    remote_name = split_id(f"{ORGANIZATION}/{NAMESPACE}/{REMOTE_AGENT}")
    await gateway.set_route(remote_name)

    logger.info(f"Connected to SLIM gateway at {address}")

    # Initialize SLIM connector
    # slim_connector = SLIMConnector(
    #     remote_org="cisco",
    #     remote_namespace="default",
    #     shared_space="chat",
    # )

    # # Register agents with the connector
    # slim_connector.register("remote_client_agent")

    # Instrument SLIM communications
    SLIMInstrumentor().instrument()

    return gateway


async def create_session(gateway):
    """
    Create a session for communication with the remote agent
    """
    session_info = await gateway.create_session(
        slim_bindings.PySessionConfiguration.FireAndForget(
            max_retries=5,
            timeout=datetime.timedelta(seconds=30),
        )
    )

    logger.info(f"Created client session: {session_info.id}")

    return session_info


# Build the state graph
# @graph_decorator(name="remote_client_agent_graph")
def build_graph() -> Any:
    """
    Constructs the state graph for handling requests.

    Returns:
        StateGraph: A compiled LangGraph state graph.
    """
    builder = StateGraph(GraphState)
    builder.add_node("node_remote_request_stateless", node_remote_slim)
    builder.add_edge(START, "node_remote_request_stateless")
    builder.add_edge("node_remote_request_stateless", END)
    return builder.compile()


def init_gateway_conn():
    """
    Initialize the gateway connection and session
    """
    port = os.getenv("PORT", "46357")
    address = os.getenv("SLIM_ADDRESS", "127.0.0.1")
    full_address = f"http://{address}:{port}"

    # Connect to gateway
    GatewayHolder.gateway = asyncio.run(connect_to_gateway(full_address))

    # Create session
    GatewayHolder.session_info = asyncio.run(create_session(GatewayHolder.gateway))

    logger.info("Gateway connection and session initialized successfully")


def main():
    load_environment_variables()
    init_gateway_conn()

    session_start()  # entry point in execution

    graph = build_graph()

    inputs = {"messages": [HumanMessage(content="Write a story about a cat")]}
    logger.info({"event": "invoking_graph", "inputs": inputs})
    result = graph.invoke(inputs)
    logger.info({"event": "final_result", "result": result})


# Main execution
if __name__ == "__main__":
    main()
