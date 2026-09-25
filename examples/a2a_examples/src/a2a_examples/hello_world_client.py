import logging

from a2a.client import create_client
from a2a.helpers import get_stream_response_text, new_text_message
from a2a.types import Role, SendMessageRequest

from ioa_observe.sdk import Observe
from ioa_observe.sdk.instrumentations.a2a import A2AInstrumentor
from ioa_observe.sdk.tracing import session_start


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    Observe.init("helloworld_a2a_client", api_endpoint="http://localhost:4318")
    A2AInstrumentor().instrument()

    base_url = "http://localhost:9999"
    logger.info("Connecting to %s", base_url)
    client = await create_client(base_url)
    session_start()

    request = SendMessageRequest(
        message=new_text_message(
            "how much is 10 USD in INR?",
            role=Role.ROLE_USER,
        )
    )
    async for chunk in client.send_message(request):
        print(get_stream_response_text(chunk))
    await client.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
