"""
FastMCP quickstart example.

cd to the `examples/snippets/clients` directory and run:
    uv run server fastmcp_quickstart stdio
"""

import os

from mcp.server.fastmcp import FastMCP
from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import agent, graph
from ioa_observe.sdk.instrumentations.mcp import McpInstrumentor
from ioa_observe.sdk.tracing import session_start
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


# Add a prompt
@mcp.prompt()
def greet_user(name: str, style: str = "friendly") -> str:
    """Generate a greeting prompt"""
    styles = {
        "friendly": "Please write a warm, friendly greeting",
        "formal": "Please write a formal, professional greeting",
        "casual": "Please write a casual, relaxed greeting",
    }

    return f"{styles.get(style, styles['friendly'])} for someone named {name}."


if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport="streamable-http")
