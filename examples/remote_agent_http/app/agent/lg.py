# Build the state graph

import logging
import os
import sys
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.messages.utils import convert_to_openai_messages
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from ioa_observe.sdk import Observe

from ioa_observe.sdk.decorators import graph as graph_decorator, agent

# Get the absolute path of the parent directory
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Add the parent directory to sys.path
sys.path.insert(0, parent_dir)

from agent.prompts import Prompts # noqa: E402
from core.logging_config import configure_logging  # noqa: E402

serviceName = "remote-server-agent_http"
Observe.init(serviceName, api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))


logger = logging.getLogger(__file__)


# Define the graph state
class GraphState(TypedDict):
    """Represents the state of the graph, containing a list of messages."""

    messages: Annotated[List[BaseMessage], add_messages]


# Graph node that makes a stateless request to the Remote Graph Server
def end_node(state: GraphState) -> Dict[str, Any]:
    logger.info(f"Thread end: {state.values()}")
    return {"messages": []}

@agent(name="remote_llm_node")
def llm_node(state: GraphState) -> Dict[str, Any]:
    """
    Creates a plan to solve the user's request

    Args:
        state (State): The current conversation state containing messages and rounds.

    Returns:
        State: The updated state with the assistant's response and incremented rounds.

    Notes:
        - Uses the ChatOpenAI model to generate the assistant's reply.
        - If an error occurs, logs the error and returns a default state.
    """
    prompt = ChatPromptTemplate(
        [
            (
                "system",
                "{system_prompt}",
            ),
            MessagesPlaceholder(variable_name="messages"),
        ]
    )
    partial_prompt = prompt.partial(system_prompt=Prompts.SYSTEM)
    base_url = os.getenv("LLM_BASE_URL")
    if base_url is not None and base_url != "":
        llm = ChatOpenAI(
                model=os.getenv("OPENAI_MODEL_NAME", "gpt-4o"),
                temperature=1.0,
                base_url=base_url,
            )
    else:
        llm = ChatOpenAI(
                model=os.getenv("OPENAI_MODEL_NAME", "gpt-4o"),
                temperature=1.0,
            )
    generate = partial_prompt | llm

    try:
        llm_response = generate.invoke({"messages": state["messages"]})
        return {"messages": [llm_response]}
    except RuntimeError as e:
        logger.error(f"Error in generation_node: {e}")
        return {"messages": []}

@graph_decorator(name="remote_server_agent_graph")
def build_graph() -> Any:
    """
    Constructs the state graph for handling requests.

    Returns:
        StateGraph: A compiled LangGraph state graph.
    """
    builder = StateGraph(GraphState)
    builder.add_node("llm_node", llm_node)
    builder.add_node("end_node", end_node)
    builder.add_edge(START, "llm_node")
    builder.add_edge("llm_node", "end_node")
    builder.add_edge("end_node", END)
    return builder.compile(name="slim_remote_graph")


def invoke_graph(
    messages: List[Dict[str, str]], graph: Optional[Any] = None
) -> Optional[dict[Any, Any] | list[dict[Any, Any]]]:
    """
    Invokes the graph with the given messages and safely extracts the last AI-generated message.

    - Logs errors if keys or indices are missing.
    - Ensures the graph is initialized if not provided.
    - Returns a meaningful response even if an error occurs.

    :param messages: A list of message dictionaries.
    :param graph: An optional graph object to use; will be built if not provided.
    :return: The list of all messages returned by the graph
    """
    inputs = {"messages": messages}
    logger.debug({"event": "invoking_graph", "inputs": inputs})

    try:
        if not graph:
            graph = build_graph()

        result = graph.invoke(inputs)

        if not isinstance(result, dict):
            raise TypeError(
                f"Graph invocation returned non-dict result: {type(result)}"
            )

        messages_list = convert_to_openai_messages(result.get("messages", []))
        if not isinstance(messages_list, list) or not messages_list:
            raise ValueError("Graph result does not contain a valid 'messages' list.")

        last_message = messages_list[-1]
        if not isinstance(last_message, dict) or "content" not in last_message:
            raise KeyError(f"Last message does not contain 'content': {last_message}")

        ai_message_content = last_message["content"]
        logger.info(f"AI message content: {ai_message_content}")
        return messages_list

    except Exception as e:
        logger.error(f"Error invoking graph: {e}", exc_info=True)
        return [{"role": "assistant", "content": "Error processing user message"}]


def main():
    # Initialize logger
    logger = configure_logging()
    load_dotenv(override=True)
    graph = build_graph()
    inputs = {"messages": [HumanMessage(content="Write a story about a cat")]}
    logger.info({"event": "invoking_graph", "inputs": inputs})
    result = graph.invoke(inputs)
    logger.info({"event": "final_result", "result": result})


# Main execution
if __name__ == "__main__":
    invoke_graph([{"role": "user", "content": "write a story about a cat"}])
    # main()
