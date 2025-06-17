# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import os

from langchain_openai import ChatOpenAI
from typing import Annotated, TypedDict, Literal

from langchain_core.messages import HumanMessage
from langchain_experimental.utilities import PythonREPL
from langgraph.constants import START, END
from langgraph.graph import MessagesState, StateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.types import Command
from langchain_community.tools.tavily_search import TavilySearchResults

from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import agent, graph
from ioa_observe.sdk.decorators import tool as observe_tool
from ioa_observe.sdk.tracing import session_start

# This executes code locally, which can be unsafe
repl = PythonREPL()
serviceName = "multi-agent-service"

Observe.init(serviceName, api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))

tavily_tool = TavilySearchResults(max_results=5)


@observe_tool(name="Python REPL tool")
def python_repl_tool(
    code: Annotated[str, "The python code to execute to generate your chart."],
):
    """Use this to execute python code and do math. If you want to see the output of a value,
    you should print it out with `print(...)`. This is visible to the user."""
    try:
        result = repl.run(code)
    except BaseException as e:
        return f"Failed to execute. Error: {repr(e)}"
    result_str = f"Successfully executed:\n```python\n{code}\n```\nStdout: {result}"
    return result_str


members = ["researcher", "coder"]
# Our team supervisor is an LLM node. It just picks the next agent to process
# and decides when the work is completed
options = members + ["FINISH"]

system_prompt = (
    "You are a supervisor tasked with managing a conversation between the"
    f" following workers: {members}. Given the following user request,"
    " respond with the worker to act next. Each worker will perform a"
    " task and respond with their results and status. When finished,"
    " respond with FINISH."
)


class Router(TypedDict):
    """Worker to route to next. If no workers needed, route to FINISH."""

    next: Literal["researcher", "coder", "FINISH"]


llm = ChatOpenAI(model="gpt-3.5-turbo")


class State(MessagesState):
    next: str


def chatbot(state: State):
    ans = llm.invoke(state["messages"])
    messages = {"messages": [ans]}
    return messages


@agent(name="supervisor")
def supervisor_node(state: State) -> Command[Literal["researcher", "coder", "__end__"]]:
    messages = [
        {"role": "system", "content": system_prompt},
    ] + state["messages"]
    response = llm.with_structured_output(Router).invoke(messages)
    goto = response["next"]
    if goto == "FINISH":
        goto = END

    return Command(goto=goto, update={"next": goto})


research_agent = create_react_agent(
    llm, tools=[tavily_tool], prompt="You are a researcher. DO NOT do any math."
)


@agent(name="research")
def research_node(state: State) -> Command[Literal["supervisor"]]:
    result = research_agent.invoke(state)
    return Command(
        update={
            "messages": [
                HumanMessage(content=result["messages"][-1].content, name="researcher")
            ]
        },
        goto="supervisor",
    )


# NOTE: THIS PERFORMS ARBITRARY CODE EXECUTION, WHICH CAN BE UNSAFE WHEN NOT SANDBOXED
code_agent = create_react_agent(llm, tools=[python_repl_tool])


@agent(name="code")
def code_node(state: State) -> Command[Literal["supervisor"]]:
    result = code_agent.invoke(state)
    return Command(
        update={
            "messages": [
                HumanMessage(content=result["messages"][-1].content, name="coder")
            ]
        },
        goto="supervisor",
    )


@graph(name="multi_agent_graph")
def build_graph():
    builder = StateGraph(State)
    builder.add_edge(START, "supervisor")
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("researcher", research_node)
    builder.add_node("coder", code_node)
    return builder.compile()


if __name__ == "__main__":
    while True:
        user_input = input("User: ")
        session_start()  # Start a new session for each user input i.e., entry point in execution
        if user_input:
            if user_input.lower() in ["quit", "exit", "q"]:
                print("Goodbye!")
                break
            for event in build_graph().stream(
                {"messages": [("user", user_input)]}, subgraphs=True
            ):
                print("Assistant:", event)
    # for s in graph.stream(
    #         {"messages": [("user", "What's the sum of 55 + 5678977?")]}, subgraphs=True
    # ):
    #     print(s)
    #     print("----")
