#!/usr/bin/env python3
"""SLIM v1.x Client Example - Connects to server and exchanges messages."""

import asyncio
import datetime
import logging
import os
import sys

import slim_bindings

from ioa_observe.sdk import Observe
from ioa_observe.sdk.connectors.slim import process_slim_msg
from ioa_observe.sdk.instrumentations.slim import SLIMInstrumentor
from ioa_observe.sdk.tracing.tracing import session_start

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

Observe.init("remote-client-agent", api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))
SLIMInstrumentor().instrument()


class SlimClient:
    def __init__(
        self,
        local_id="agntcy/example/client",
        remote_id="agntcy/example/server",
        endpoint="http://127.0.0.1:46357",
        secret="theawesomesharedsecretthatmustbeatleast32characters",
    ):
        self.local_id = local_id
        self.remote_id = remote_id
        self.endpoint = endpoint
        self.secret = secret
        self.service = None
        self.app = None
        self.conn_id = None

    async def connect(self):
        """Initialize and connect to server."""
        local_name = slim_bindings.Name(*self.local_id.split("/"))

        slim_bindings.initialize_with_configs(
            tracing_config=slim_bindings.new_tracing_config(),
            runtime_config=slim_bindings.new_runtime_config(),
            service_config=[slim_bindings.new_service_config()],
        )

        self.service = slim_bindings.get_global_service()
        self.app = slim_bindings.App.new_with_secret(local_name, self.secret)

        config = slim_bindings.new_insecure_client_config(self.endpoint)
        self.conn_id = await self.service.connect_async(config)
        await self.app.subscribe_async(local_name, self.conn_id)

        logger.info(f"Connected to {self.endpoint}")

    async def create_session(self):
        """Create a session with the remote peer."""
        remote_name = slim_bindings.Name(*self.remote_id.split("/"))
        await self.app.set_route_async(remote_name, self.conn_id)

        config = slim_bindings.SessionConfig(
            session_type=slim_bindings.SessionType.POINT_TO_POINT,
            enable_mls=False,
            max_retries=5,
            interval=datetime.timedelta(seconds=5),
            metadata={},
        )

        session = await self.app.create_session_and_wait_async(config, remote_name)
        logger.info(f"Session created: {session.session_id()}")
        return session

    @process_slim_msg(name="send_message")
    async def send(self, session, message: str) -> str:
        """Send message and get response."""
        await session.publish_async(message.encode("utf-8"), None, None)

        resp = await session.get_message_async(timeout=datetime.timedelta(seconds=30))
        return resp.payload.decode("utf-8")

    async def run(self, messages=None):
        """Run client with given messages."""
        await self.connect()
        session = await self.create_session()

        messages = messages or ["Hello!", "How are you?", "Goodbye!"]

        try:
            for msg in messages:
                response = await self.send(session, msg)
                print(f"Sent: {msg}\nReceived: {response}\n")
                await asyncio.sleep(0.5)
        finally:
            await self.app.delete_session_async(session)


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="SLIM Client")
    parser.add_argument("--endpoint", default="http://127.0.0.1:46357")
    parser.add_argument("--messages", nargs="+", help="Messages to send")
    args = parser.parse_args()

    session_start()

    client = SlimClient(endpoint=args.endpoint)
    await client.run(args.messages)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
