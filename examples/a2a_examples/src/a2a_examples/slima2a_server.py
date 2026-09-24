import slim_bindings
import asyncio
from a2a.server.request_handlers import DefaultRequestHandler
from slima2a import setup_slim_client
from slima2a.handler import SRPCHandler
from slima2a.types.v1 import a2a_pb2_slimrpc
from a2a_examples.agent_executor import RandomNumberAgentExecutor
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.server.tasks import InMemoryTaskStore
from ioa_observe.sdk import Observe
from ioa_observe.sdk.instrumentations.a2a import A2AInstrumentor
import os

Observe.init(
    "a2a-random-number-generator-server", api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT")
)

A2AInstrumentor().instrument()


async def main():
    skill = AgentSkill(
        id="random_number",
        name="Random Number Generator",
        description="Generates a random number between 1 and 100",
        tags=["random", "number", "utility"],
        examples=["Give me a random number", "Roll a number", "Random"],
    )
    agent_card = AgentCard(
        name="Random Number Agent",
        description="An agent that returns a random number between 1 and 100",
        supported_interfaces=[
            AgentInterface(
                protocol_binding="slimrpc",
                protocol_version="1.0",
                url="agntcy/demo/server",
            )
        ],
        default_input_modes=["text"],
        default_output_modes=["text"],
        skills=[skill],
        version="1.0.0",
        capabilities=AgentCapabilities(),
    )
    request_handler = DefaultRequestHandler(
        agent_executor=RandomNumberAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )
    servicer = SRPCHandler(agent_card, request_handler)
    _, local_app, local_name, conn_id = await setup_slim_client(
        namespace="agntcy",
        group="demo",
        name="server",
    )
    server = slim_bindings.Server.new_with_connection(
        local_app,
        local_name,
        conn_id,
    )

    a2a_pb2_slimrpc.add_A2AServiceServicer_to_server(
        servicer,
        server,
    )

    await server.serve_async()


if __name__ == "__main__":
    asyncio.run(main())
