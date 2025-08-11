import os

from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent
import asyncio
from dotenv import load_dotenv

from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import agent, graph
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
async def agent(tools, messages):
    agent = create_react_agent("gpt-4o", tools)
    return await agent.ainvoke(messages)


async def main():
    client = MultiServerMCPClient(
        {
            "math": {
                # Ensure you start your weather server on port 8000
                "url": "http://localhost:8000/mcp",
                "transport": "streamable_http",
            }
        }
    )
    tools = await client.get_tools()
    session_start()
    math_response = await agent(
        tools, {"messages": [{"role": "user", "content": "what's (3 + 5) x 12?"}]}
    )
    print(math_response)


asyncio.run(main())
