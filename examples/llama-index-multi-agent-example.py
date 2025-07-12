# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import asyncio
import os

from llama_index.core.agent.workflow import (
    AgentWorkflow,
    FunctionAgent,
)  # For type hinting
from llama_index.core.tools import FunctionTool
from llama_index.llms.openai import OpenAI

from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import graph, agent, tool
from ioa_observe.sdk.tracing import session_start

Observe.init("multi_agent_new_workflow", api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))


@tool(name="ToolOne")
def tool_one_func(query: str) -> str:
    """Tool for agent one"""
    return f"Tool one processed: {query}"


tool_one = FunctionTool.from_defaults(fn=tool_one_func)


@tool(name="ToolTwo")
def tool_two_func(data: str) -> str:
    """Tool for agent two"""
    return f"Tool two processed: {data}"


tool_two = FunctionTool.from_defaults(fn=tool_two_func)

llm = OpenAI(model="gpt-4o-mini")


@agent(name="AgentOne")
class AgentOne(FunctionAgent):
    """Agent One class for initial processing tasks."""

    def __init__(self, llm):
        super().__init__(
            name="AgentOne",
            tools=[tool_one],
            llm=llm,
            system_prompt="You are agent one.",
            can_handoff_to=["AgentTwo"],  # Explicit handoff capability
        )
        self.description = "This is Agent One, responsible for initial processing."


# Create an instance of AgentOne
agent_one = AgentOne(llm)


@agent(name="AgentTwo")
class AgentTwo(FunctionAgent):
    """Agent Two class for final processing tasks."""

    def __init__(self, llm):
        super().__init__(
            name="AgentTwo",
            tools=[tool_two],
            llm=llm,
            system_prompt="You are agent two.",
            # can_handoff_to can be empty or lead to other agents or loop back
        )
        self.description = "This is Agent Two, responsible for final processing."


# Create an instance of AgentTwo
agent_two = AgentTwo(llm)


@graph(name="multi_agent_workflow_graph")
@agent(name="multi_agent_workflow")
class MultiAgentWorkflow(AgentWorkflow):
    """Custom workflow class that manages multiple agents."""

    def __init__(self):
        super().__init__(agents=[agent_one, agent_two], root_agent="AgentOne")

    async def run(self, user_msg: str):
        return await super().run(user_msg)


# Create an instance of our custom workflow
session_start()  # Start a new session for tracing
multi_agent_workflow_instance = MultiAgentWorkflow()
response = asyncio.run(
    multi_agent_workflow_instance.run(
        user_msg="Can you analyze this data for me? Lorem ipsum dolor sit amet, consectetur adipiscing elit."
    )
)
print(f"Final response: {response}")
