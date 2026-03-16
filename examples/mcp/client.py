"""
MCP Client example using LangChain MCP adapters with Observe SDK instrumentation.

Prerequisites:
    1. Start the MCP server: python server.py
    2. Set environment variables (see .env.example)

Usage:
    python client.py
"""

import os

from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent
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
    react_agent = create_react_agent("gpt-4o", tools)
    return await react_agent.ainvoke(messages)


async def main():
    client = MultiServerMCPClient(
        {
            "math": {
                "url": "http://localhost:8000/mcp",
                "transport": "streamable_http",
            }
        }
    )
    tools = await client.get_tools()
    session_start()
    math_response = await math_agent_fn(
        tools, {"messages": [{"role": "user", "content": "what's (3 + 5) x 12?"}]}
    )
    print(math_response)


asyncio.run(main())
