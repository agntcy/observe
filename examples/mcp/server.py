"""
MCP Server example with Observe SDK instrumentation.

Start the server:
    python server.py

The server will start on http://localhost:8000/mcp using Streamable HTTP transport.
"""

import os

from mcp.server.fastmcp import FastMCP
from ioa_observe.sdk import Observe
from ioa_observe.sdk.instrumentations.mcp import McpInstrumentor
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


# Create an MCP server
mcp = FastMCP("Demo")

serviceName = "mcp_server"

Observe.init(serviceName, api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))

McpInstrumentor().instrument()


# Add an addition tool
@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers"""
    return a + b


@mcp.tool()
def multiply(a: int, b: int) -> int:
    """Multiply two numbers"""
    return a * b


# Add a dynamic greeting resource
@mcp.resource("greeting://{name}")
def get_greeting(name: str) -> str:
    """Get a personalized greeting"""
    return f"Hello, {name}!"




if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport="streamable-http")
