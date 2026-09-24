from a2a.client import minimal_agent_card
from a2a.helpers import get_stream_response_text, new_text_message
from slima2a import setup_slim_client
from slima2a.client_transport import (
    ClientConfig,
    MultiAgentClientFactory,
    slimrpc_channel_factory,
)
import asyncio
import httpx
from a2a.types import Role, SendMessageRequest

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
    _, local_app, _, conn_id = await setup_slim_client(
        namespace="agntcy",
        group="demo",
        name="client",
    )

    httpx_client = httpx.AsyncClient()
    client_config = ClientConfig(
        supported_protocol_bindings=["slimrpc"],
        streaming=True,
        httpx_client=httpx_client,
        slimrpc_channel_factory=slimrpc_channel_factory(local_app, conn_id),
    )
    client_factory = MultiAgentClientFactory(client_config)

    ac = minimal_agent_card("agntcy/demo/server", ["slimrpc"])
    client = client_factory.create(ac)

    request = SendMessageRequest(
        message=new_text_message(
            "Give me a random number",
            role=Role.ROLE_USER,
        )
    )
    try:
        response = client.send_message(request)
        async for event_or_message in response:
            print(get_stream_response_text(event_or_message))
    finally:
        await client.close()
        await httpx_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
