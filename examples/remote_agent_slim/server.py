#!/usr/bin/env python3
"""SLIM v1.x Server Example - Listens for sessions and responds using OpenAI."""

import asyncio
import datetime
import logging
import os
import signal

import slim_bindings
from dotenv import load_dotenv
from openai import AsyncOpenAI

from ioa_observe.sdk import Observe
from ioa_observe.sdk.connectors.slim import process_slim_msg
from ioa_observe.sdk.instrumentations.slim import SLIMInstrumentor

load_dotenv()
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

Observe.init("remote-server-agent", api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))
SLIMInstrumentor().instrument()


class SlimServer:
    def __init__(
        self,
        identity="agntcy/example/server",
        endpoint="127.0.0.1:46357",
        secret="theawesomesharedsecretthatmustbeatleast32characters",
    ):
        self.identity = identity
        self.endpoint = endpoint
        self.secret = secret
        self.service = None
        self.app = None
        self.running = False

        # OpenAI client (optional)
        api_key = os.getenv("OPENAI_API_KEY")
        self.openai = AsyncOpenAI(api_key=api_key) if api_key else None

    async def initialize(self):
        """Initialize SLIM service and app."""
        name = slim_bindings.Name(*self.identity.split("/"))

        slim_bindings.initialize_with_configs(
            tracing_config=slim_bindings.new_tracing_config(),
            runtime_config=slim_bindings.new_runtime_config(),
            service_config=[slim_bindings.new_service_config()],
        )

        self.service = slim_bindings.get_global_service()
        self.app = slim_bindings.App.new_with_secret(name, self.secret)
        logger.info(f"Initialized app: {self.app.id()}")

    async def start(self):
        """Start the server."""
        config = slim_bindings.new_insecure_server_config(self.endpoint)
        await self.service.run_server_async(config)
        self.running = True
        logger.info(f"Server running on {self.endpoint}")

    def stop(self):
        """Stop the server."""
        if self.running:
            self.service.stop_server(self.endpoint)
            self.running = False
            logger.info("Server stopped")

    @process_slim_msg(name="process_message")
    async def process(self, message: str, session_id: str) -> str:
        """Process a message - use OpenAI if available, else echo."""
        if self.openai:
            try:
                resp = await self.openai.chat.completions.create(
                    model="gpt-4",
                    messages=[{"role": "user", "content": message}],
                    max_tokens=200,
                )
                return resp.choices[0].message.content.strip()
            except Exception as e:
                logger.error(f"OpenAI error: {e}")
        return f"Echo: {message}"

    async def handle_session(self, session):
        """Handle messages for a session."""
        try:
            sid = session.session_id()
            logger.info(f"Session started: {sid}")

            while True:
                msg = await session.get_message_async(
                    timeout=datetime.timedelta(seconds=30)
                )
                text = msg.payload.decode("utf-8")

                response = await self.process(text, str(sid))
                await session.publish_to_async(
                    msg.context, response.encode("utf-8"), None, None
                )

        except Exception as e:
            logger.error(f"Session error: {e}")

    async def listen(self):
        """Listen for incoming sessions."""
        loop = asyncio.get_event_loop()

        while self.running:
            try:
                # Use shorter timeout so we can check self.running more frequently
                session = await loop.run_in_executor(
                    None,
                    lambda: self.app.listen_for_session(
                        timeout=datetime.timedelta(seconds=5)
                    ),
                )
                asyncio.create_task(self.handle_session(session))
            except Exception as e:
                err_str = str(e).lower()
                if "timeout" in err_str or "timed out" in err_str:
                    continue  # Normal timeout, keep listening
                if self.running:
                    logger.error(f"Listen error: {e}")
                break

    async def run(self):
        """Run the server."""
        await self.initialize()
        await self.start()
        await self.listen()
        self.stop()


async def main():
    server = SlimServer()

    def shutdown():
        logger.info("Shutting down...")
        server.running = False

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown)

    try:
        await server.run()
    except asyncio.CancelledError:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
