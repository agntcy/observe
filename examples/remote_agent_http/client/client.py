# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import uuid
import requests
from typing import Annotated, Any, Dict, List, TypedDict

from dotenv import load_dotenv, find_dotenv
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.messages.utils import convert_to_openai_messages
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from ioa_observe.sdk import Observe
from ioa_observe.sdk.tracing import session_start
from ioa_observe.sdk.decorators import graph as graph_decorator, agent
from ioa_observe.sdk.tracing.context_utils import get_current_context_headers
from ioa_observe.sdk import TracerWrapper

from logging_config import configure_logging

logger = configure_logging()

serviceName = "remote-client-agent_http"
Observe.init(serviceName, api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))


def load_environment_variables(env_file: str | None = None) -> None:
    env_path = env_file or find_dotenv()
    if env_path:
        load_dotenv(env_path, override=True)
        logger.info(f".env file loaded from {env_path}")
    else:
        logger.warning("No .env file found. Ensure environment variables are set.")


def decode_response(response_data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        agent_id = response_data.get("agent_id", "Unknown")
        output = response_data.get("output", {})
        model = response_data.get("model", "Unknown")
        metadata = response_data.get("metadata", {})
        messages = output.get("messages", [])
        return {
            "agent_id": agent_id,
            "messages": messages,
            "model": model,
            "metadata": metadata,
        }
    except Exception as e:
        return {"error": f"Failed to decode response: {str(e)}"}


class GraphState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    session_id: dict


def send_http_request(payload: Dict[str, Any], headers: dict) -> Dict[str, Any]:
    endpoint = os.getenv("REMOTE_SERVER_URL", "http://localhost:5000/runs")
    try:
        response = requests.post(endpoint, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"HTTP request failed: {e}")
        return {"messages": [HumanMessage(content=f"HTTP Error: {str(e)}")]}


@agent(name="remote_client_agent_http")
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

    # Use decode_response to extract messages from the nested 'output'
    parsed = decode_response(response_data)
    if "messages" not in parsed:
        logger.error("Invalid response format")
        return {"messages": [HumanMessage(content="Error: Invalid response from server")]}

    return {"messages": parsed["messages"]}

@graph_decorator(name="remote_client_graph")
def build_graph() -> Any:
    builder = StateGraph(GraphState)
    builder.add_node("node_remote_request_http", node_remote_http)
    builder.add_edge(START, "node_remote_request_http")
    builder.add_edge("node_remote_request_http", END)
    return builder.compile()


def main():
    load_environment_variables()
    # using session_start as a context manager to ensure proper session handling
    with session_start() as s:
        graph = build_graph()
        inputs = {"messages": [HumanMessage(content="Write a story about a cat")], "session_id": s}
        logger.info({"event": "invoking_graph", "inputs": inputs})
        result = graph.invoke(inputs)
        logger.info({"event": "final_result", "result": result})


if __name__ == "__main__":
    main()
