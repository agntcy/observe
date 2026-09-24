import os
import uuid
from a2a.client import create_client
from a2a.helpers import get_stream_response_text
from a2a.types import (
    Message,
    Part,
    Role,
    SendMessageRequest,
)

from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import graph
from ioa_observe.sdk.instrumentations.a2a import A2AInstrumentor
from ioa_observe.sdk.tracing import session_start

BASE_URL = "http://localhost:9999"

Observe.init(
    "a2a-random-number-generator-client", api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT")
)

A2AInstrumentor().instrument()


@graph(name="get_agents")
def get_agents() -> list:
    """Returns a list of agents that we can register with ioa_observe SDK.
     We have two agents:
    - a2a-random-number-generator-client: The client that sends requests to the server.
    - a2a-random-number-generator-server: The server that processes requests, finds an appropriate agent, and returns a response.
    """
    return ["a2a-random-number-generator-client", "a2a-random-number-generator-server"]


async def main() -> None:
    client = await create_client(BASE_URL)
    session_start()
    get_agents()

    message = Message(
        role=Role.ROLE_USER,
        message_id=str(uuid.uuid4()),
        parts=[Part(text="Give me a random number")],
    )

    print("Sending message...")
    async for response in client.send_message(SendMessageRequest(message=message)):
        print(get_stream_response_text(response))
    await client.close()


def entrypoint():
    import asyncio

    asyncio.run(main())


if __name__ == "__main__":
    entrypoint()
