import os
import uuid
import httpx
from a2a.client import A2ACardResolver, A2AClient
from a2a.types import (
    AgentCard,
    Message,
    MessageSendParams,
    Part,
    Role,
    SendMessageRequest,
    TextPart,
)

from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import graph
from ioa_observe.sdk.instrumentations.a2a import A2AInstrumentor
from ioa_observe.sdk.tracing import session_start

PUBLIC_AGENT_CARD_PATH = "/.well-known/agent.json"
BASE_URL = "http://localhost:9999"

Observe.init("a2a-random-number-generator-client", api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))

A2AInstrumentor().instrument()

@graph(name="get_agents")
def get_agents() -> list:
    """ Returns a list of agents that we can register with ioa_observe SDK.
     We have two agents:
    - a2a-random-number-generator-client: The client that sends requests to the server.
    - a2a-random-number-generator-server: The server that processes requests, finds an appropriate agent, and returns a response.
    """
    return ["a2a-random-number-generator-client", "a2a-random-number-generator-server"]

async def main() -> None:
    async with httpx.AsyncClient() as httpx_client:
        # Fetch the agent card
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=BASE_URL)
        try:
            print(f"Fetching public agent card from: {BASE_URL}{PUBLIC_AGENT_CARD_PATH}")
            agent_card: AgentCard = await resolver.get_agent_card()
            print("Agent card fetched successfully:")
            print(agent_card.model_dump_json(indent=2))
        except Exception as e:
            print(f"Error fetching public agent card: {e}")
            return

        # Initialize A2A client with the agent card
        client = A2AClient(httpx_client=httpx_client, agent_card=agent_card)

        session_start()
        # make sure the agents are registered with ioa_observe SDK
        get_agents()

        # Build message
        message_payload = Message(
            role=Role.user,
            messageId=str(uuid.uuid4()),
            parts=[Part(root=TextPart(text="Give me a random number"))],
        )
        request = SendMessageRequest(
            id=str(uuid.uuid4()),
            params=MessageSendParams(message=message_payload),
        )

        # Send message
        print("Sending message...")
        
        response = await client.send_message(request)

        # Print response
        print("Response:")
        print(response.model_dump_json(indent=2))


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())