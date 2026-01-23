import slimrpc
from a2a.client import ClientFactory, minimal_agent_card
from slima2a.client_transport import SRPCTransport, ClientConfig
import asyncio
import httpx
import uuid
from a2a.types import Message, Role, Part, DataPart

from ioa_observe.sdk import Observe
from ioa_observe.sdk.instrumentations.a2a import A2AInstrumentor
from ioa_observe.sdk.tracing import session_start
import os

Observe.init(
    "a2a-random-number-generator-client", api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT")
)

A2AInstrumentor().instrument()


async def main():
    session_start()
    local_app = await slimrpc.common.create_local_app(
        slimrpc.SLIMAppConfig(
            identity="agntcy/demo/client",
            slim_client_config={
                "endpoint": "http://localhost:46357",
                "tls": {
                    "insecure": True,
                },
            },
            shared_secret="secretverylongbecause32isratherlong",
        )
    )

    def channel_factory(topic) -> slimrpc.Channel:
        channel = slimrpc.Channel(
            local_app=local_app,
            remote="agntcy/demo/server",
        )
        return channel

    httpx_client = httpx.AsyncClient()
    client_config = ClientConfig(
        supported_transports=["slimrpc"],
        streaming=True,
        httpx_client=httpx_client,
        slimrpc_channel_factory=channel_factory,
    )
    client_factory = ClientFactory(client_config)
    client_factory.register("slimrpc", SRPCTransport.create)

    ac = minimal_agent_card("agntcy/demo/server", ["slimrpc"])
    client = client_factory.create(ac)

    try:
        # Build message
        message_payload = Message(
            role=Role.user,
            message_id=str(uuid.uuid4()),
            parts=[Part(root=DataPart(data={"text": "Give me a random number"}))],
        )

        response = client.send_message(message_payload)
        async for event_or_message in response:
            print(event_or_message)
    except slimrpc.SRPCResponseError as e:
        print(f"ERROR! {e}")


asyncio.run(main())
