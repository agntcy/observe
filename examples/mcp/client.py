"""
MCP Client example using LangChain MCP adapters with Observe SDK instrumentation.

Prerequisites:
    1. Start the MCP server: python server.py
    2. Set environment variables (see .env.example)

Usage:
    python client.py
"""

import os

from langchain.agents import create_agent
from langchain.mcp import MCPAdapter
import asyncio
from dotenv import load_dotenv

from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import agent
from ioa_observe.sdk.instrumentations.mcp import McpInstrumentor
from ioa_observe.sdk.tracing import session_start

# Load environment variables from .env file
load_dotenv()

serviceName = "mcp_client"
Observe.init(serviceName, api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))

McpInstrumentor().instrument()


@agent(
    name="math_agent",
    description="An agent that can perform mathematical operations using MCP tools.",
)
async def math_agent_fn(tools, messages):
    react_agent = create_agent("openai:gpt-4.1-mini", tools)
    return await react_agent.ainvoke(messages)


async def main():
    config = {
        "mcpServers": {
            "math": {
                "url": "http://127.0.0.1:8000/mcp",
            }
        }
    }
    async with MCPAdapter(config) as adapter:
        tools = await adapter.list_tools()
        session_start()
        math_response = await math_agent_fn(
            tools,
            {"messages": [{"role": "user", "content": "what's (3 + 5) x 12?"}]},
        )
    print(math_response)


if __name__ == "__main__":
    asyncio.run(main())
