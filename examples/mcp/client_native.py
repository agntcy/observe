"""
Native MCP Client example using the MCP Python SDK directly with Observe SDK instrumentation.

This example uses the MCP SDK's ClientSession and streamable_http_client directly,
without any LangChain dependencies.

Prerequisites:
    1. Start the MCP server: python server.py
    2. Set environment variables (see .env.example)

Usage:
    python client_native.py
"""

import asyncio
import os

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import agent
from ioa_observe.sdk.instrumentations.mcp import McpInstrumentor
from ioa_observe.sdk.tracing import session_start

# Load environment variables from .env file
load_dotenv()

Observe.init("mcp_native_client", api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))
McpInstrumentor().instrument()


@agent(
    name="math_agent",
    description="An agent that calls MCP tools to perform math operations.",
)
async def math_agent(url: str):
    """Connect to an MCP server and call its tools."""
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # List available tools
            tools_result = await session.list_tools()
            print(f"Available tools: {[t.name for t in tools_result.tools]}")

            # Call the add tool
            add_result = await session.call_tool("add", {"a": 3, "b": 5})
            print(f"add(3, 5) = {add_result.content[0].text}")

            # Call the multiply tool
            multiply_result = await session.call_tool("multiply", {"a": 8, "b": 12})
            print(f"multiply(8, 12) = {multiply_result.content[0].text}")

            return {
                "add_result": add_result.content[0].text,
                "multiply_result": multiply_result.content[0].text,
            }


async def main():
    session_start()
    result = await math_agent("http://localhost:8000/mcp")
    print(f"\nFinal result: {result}")


if __name__ == "__main__":
    asyncio.run(main())



