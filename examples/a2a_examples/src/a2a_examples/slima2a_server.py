import slimrpc
import asyncio
from a2a.server.request_handlers import DefaultRequestHandler
from slima2a.handler import SRPCHandler
from slima2a.types import a2a_pb2_slimrpc
from a2a_examples.agent_executor import RandomNumberAgentExecutor
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.server.tasks import InMemoryTaskStore
from ioa_observe.sdk import Observe
from ioa_observe.sdk.instrumentations.a2a import A2AInstrumentor
import os

Observe.init(
    "a2a-random-number-generator-server", api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT")
)

A2AInstrumentor().instrument()


async def main():
    agent_executor = RandomNumberAgentExecutor()
    request_handler = DefaultRequestHandler(
        agent_executor=agent_executor, task_store=InMemoryTaskStore()
    )

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
        url="http://localhost:9999/",
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        skills=[skill],
        version="1.0.0",
        capabilities=AgentCapabilities(),
    )
    servicer = SRPCHandler(agent_card, request_handler)
    local_app = await slimrpc.common.create_local_app(
        slimrpc.SLIMAppConfig(
            identity="agntcy/demo/server",
            slim_client_config={
                "endpoint": "http://localhost:46357",
                "tls": {
                    "insecure": True,
                },
            },
            shared_secret="secretverylongbecause32isratherlong",
        )
    )
    server = slimrpc.Server(local_app)

    a2a_pb2_slimrpc.add_A2AServiceServicer_to_server(
        servicer,
        server,
    )

    await server.run()


asyncio.run(main())
