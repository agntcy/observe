import os

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a_examples.agent_executor import RandomNumberAgentExecutor
from starlette.applications import Starlette

from ioa_observe.sdk import Observe
from ioa_observe.sdk.instrumentations.a2a import A2AInstrumentor

Observe.init(
    "a2a-random-number-generator-server", api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT")
)

A2AInstrumentor().instrument()


def main():
    # Define the skill metadata
    skill = AgentSkill(
        id="random_number",
        name="Random Number Generator",
        description="Generates a random number between 1 and 100",
        tags=["random", "number", "utility"],
        examples=["Give me a random number", "Roll a number", "Random"],
    )

    # Define the agent metadata
    agent_card = AgentCard(
        name="Random Number Agent",
        description="An agent that returns a random number between 1 and 100",
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version="1.0",
                url="http://localhost:9999/a2a/jsonrpc",
            )
        ],
        default_input_modes=["text"],
        default_output_modes=["text"],
        skills=[skill],
        version="1.0.0",
        capabilities=AgentCapabilities(),
    )

    # Configure the request handler with our custom agent executor
    request_handler = DefaultRequestHandler(
        agent_executor=RandomNumberAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    app = Starlette(
        routes=[
            *create_agent_card_routes(agent_card),
            *create_jsonrpc_routes(request_handler, rpc_url="/a2a/jsonrpc"),
        ]
    )

    uvicorn.run(app, host="0.0.0.0", port=9999)


if __name__ == "__main__":
    main()
