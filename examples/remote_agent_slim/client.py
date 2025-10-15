#!/usr/bin/env python3
"""
SLIM v0.6.0 Client Example

This client demonstrates how to use SLIM v0.6.0 Python bindings to:
1. Connect to a SLIM server
2. Create point-to-point sessions with a remote peer
3. Send messages and receive responses
4. Use the observe SDK for monitoring

Based on SLIM v0.6.0 documentation and examples.
"""

import asyncio
import argparse
import json
import logging
import sys
import datetime
from typing import Optional
import os
from ioa_observe.sdk.tracing.tracing import session_start
import slim_bindings
from ioa_observe.sdk import Observe
from ioa_observe.sdk.connectors.slim import process_slim_msg
from ioa_observe.sdk.instrumentations.slim import SLIMInstrumentor
# from ioa_observe.sdk import observe
# from ioa_observe.sdk.decorators import ai_agent_action

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

serviceName = "remote-client-agent"
Observe.init(serviceName, api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))

SLIMInstrumentor().instrument()


class SlimClient:
    """
    SLIM Client using v0.6.0 bindings.

    This client:
    - Connects to a SLIM server endpoint
    - Creates point-to-point sessions with remote peers
    - Sends messages and handles responses
    - Integrates with the observe SDK for monitoring
    """

    def __init__(
        self,
        local_identity: str = "agntcy/example/client",
        remote_identity: str = "agntcy/example/server",
        server_endpoint: str = "http://127.0.0.1:46357",
        shared_secret: str = "secret",
        enable_opentelemetry: bool = False,
        enable_mls: bool = False,
    ):
        self.local_identity = local_identity
        self.remote_identity = remote_identity
        self.server_endpoint = server_endpoint
        self.shared_secret = shared_secret
        self.enable_opentelemetry = enable_opentelemetry
        self.enable_mls = enable_mls
        self.slim_app: Optional[slim_bindings.Slim] = None

    def _split_identity(self, identity: str) -> slim_bindings.PyName:
        """Split identity string into PyName components."""
        try:
            org, namespace, app = identity.split("/")
            return slim_bindings.PyName(org, namespace, app)
        except ValueError as e:
            raise ValueError(
                f"Identity must be in format 'org/namespace/app', got: {identity}"
            ) from e

    def _create_identity_providers(self, identity: str, secret: str):
        """Create identity provider and verifier for shared secret auth."""
        provider = slim_bindings.PyIdentityProvider.SharedSecret(
            identity=identity, shared_secret=secret
        )
        verifier = slim_bindings.PyIdentityVerifier.SharedSecret(
            identity=identity, shared_secret=secret
        )
        return provider, verifier

    async def initialize(self):
        """Initialize SLIM application and connect to server."""
        # Initialize tracing
        await slim_bindings.init_tracing(
            {
                "log_level": "info",
                "opentelemetry": {
                    "enabled": self.enable_opentelemetry,
                    "grpc": {"endpoint": "http://localhost:4317"},
                },
            }
        )

        # Create identity components
        provider, verifier = self._create_identity_providers(
            self.local_identity, self.shared_secret
        )

        # Convert identity to PyName
        local_name = self._split_identity(self.local_identity)

        # Create SLIM application
        self.slim_app = await slim_bindings.Slim.new(local_name, provider, verifier)

        logger.info(f"Created SLIM app with ID: {self.slim_app.id_str}")

        # Connect to server
        connection_config = {
            "endpoint": self.server_endpoint,
            "tls": {"insecure": True},
        }

        await self.slim_app.connect(connection_config)
        logger.info(f"Connected to SLIM server at {self.server_endpoint}")

    async def disconnect(self):
        """Disconnect from the SLIM server."""
        if self.slim_app:
            await self.slim_app.disconnect(self.server_endpoint)
            logger.info("Disconnected from SLIM server")

    # @ai_agent_action(name="send_message")
    async def send_message(self, message: str, session_id: str) -> str:
        """
        Send a message through SLIM with observe SDK monitoring.

        Args:
            message: The message to send
            session_id: The session identifier

        Returns:
            Response from the remote peer
        """
        logger.info(f"Sending message to session {session_id}: {message}")
        return message

    async def create_point_to_point_session(self):
        """Create a point-to-point session with the remote peer."""
        if not self.slim_app:
            raise RuntimeError("SLIM app not initialized. Call initialize() first.")

        # Convert remote identity to PyName
        remote_name = self._split_identity(self.remote_identity)

        # Set route to remote peer
        await self.slim_app.set_route(remote_name)
        logger.info(f"Set route to remote peer: {self.remote_identity}")

        # Create point-to-point session configuration
        session_config = slim_bindings.PySessionConfiguration.PointToPoint(
            peer_name=remote_name,
            max_retries=5,
            timeout=datetime.timedelta(seconds=5),
            mls_enabled=self.enable_mls,
            metadata={"client_id": self.local_identity},
        )

        # Create session
        session = await self.slim_app.create_session(session_config)
        session_id = getattr(session, "id", "unknown")

        logger.info(f"Created point-to-point session: {session_id}")
        return session

    async def send_message_and_wait_response(self, session, message: str) -> str:
        """Send a message and wait for response."""
        session_id = getattr(session, "id", "unknown")

        # Monitor with observe SDK
        await self.send_message(message, session_id)

        # Send message
        await session.publish(message.encode("utf-8"))
        logger.info(f"Sent message: {message}")

        # Wait for response
        msg_ctx, payload = await session.get_message()
        response = payload.decode("utf-8")

        logger.info(f"Received response: {response}")
        return response

    async def run_interactive_session(self):
        """Run an interactive session where user can input messages."""
        session = await self.create_point_to_point_session()

        try:
            print("\n--- Interactive SLIM Client ---")
            print("Type messages to send to the server. Type 'quit' to exit.\n")

            while True:
                try:
                    # Get user input
                    message = input("Enter message: ").strip()

                    if message.lower() in ["quit", "exit", "q"]:
                        break

                    if not message:
                        continue

                    # Send message and get response
                    response = await self.send_message_and_wait_response(
                        session, message
                    )
                    print(f"Response: {response}\n")

                except EOFError:
                    break
                except Exception as e:
                    logger.error(f"Error sending message: {e}")

        finally:
            # Clean up session
            await self.slim_app.delete_session(session)
            logger.info("Session deleted")

    async def run_batch_messages(self, messages: list, iterations: int = 1):
        """Send a batch of messages multiple times."""
        session = await self.create_point_to_point_session()

        try:
            for iteration in range(iterations):
                logger.info(f"Starting iteration {iteration + 1}/{iterations}")

                for i, message in enumerate(messages):
                    try:
                        response = await self.send_message_and_wait_response(
                            session, message
                        )
                        print(
                            f"[{iteration + 1}/{iterations}][{i + 1}/{len(messages)}] Sent: {message}"
                        )
                        print(
                            f"[{iteration + 1}/{iterations}][{i + 1}/{len(messages)}] Received: {response}"
                        )

                        # Small delay between messages
                        await asyncio.sleep(0.5)

                    except Exception as e:
                        logger.error(f"Error in message {i + 1}: {e}")

                if iteration < iterations - 1:
                    print(
                        f"Completed iteration {iteration + 1}, waiting before next..."
                    )
                    await asyncio.sleep(1)

        finally:
            # Clean up session
            await self.slim_app.delete_session(session)
            logger.info("Session deleted")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="SLIM v0.6.0 Client Example")
    parser.add_argument(
        "--server-endpoint",
        type=str,
        default="http://127.0.0.1:46357",
        help="SLIM server endpoint",
    )
    parser.add_argument(
        "--local-identity",
        type=str,
        default="agntcy/example/client",
        help="Local identity in format org/namespace/app",
    )
    parser.add_argument(
        "--remote-identity",
        type=str,
        default="agntcy/example/server",
        help="Remote identity to connect to",
    )
    parser.add_argument(
        "--shared-secret",
        type=str,
        default="secret",
        help="Shared secret for authentication (dev only)",
    )
    parser.add_argument(
        "--enable-opentelemetry",
        action="store_true",
        help="Enable OpenTelemetry tracing",
    )
    parser.add_argument(
        "--enable-mls", action="store_true", help="Enable MLS (Message Layer Security)"
    )
    parser.add_argument(
        "--message", type=str, help="Single message to send (batch mode)"
    )
    parser.add_argument(
        "--messages", nargs="+", help="Multiple messages to send (batch mode)"
    )
    parser.add_argument(
        "--iterations", type=int, default=1, help="Number of iterations for batch mode"
    )
    parser.add_argument(
        "--interactive", action="store_true", help="Run in interactive mode"
    )

    args = parser.parse_args()

    # Initialize observe SDK
    # observe.configure()
    session_start()

    # Create client
    client = SlimClient(
        local_identity=args.local_identity,
        remote_identity=args.remote_identity,
        server_endpoint=args.server_endpoint,
        shared_secret=args.shared_secret,
        enable_opentelemetry=args.enable_opentelemetry,
        enable_mls=args.enable_mls,
    )

    try:
        await client.initialize()

        if args.interactive:
            # Interactive mode
            await client.run_interactive_session()
        else:
            # Batch mode
            messages = []
            if args.message:
                messages = [args.message]
            elif args.messages:
                messages = args.messages
            else:
                # Default messages
                messages = [
                    "Hello from SLIM client!",
                    "How are you?",
                    "Testing message exchange",
                    "Goodbye!",
                ]

            await client.run_batch_messages(messages, args.iterations)

    except Exception as e:
        logger.error(f"Client error: {e}")
        raise
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nClient interrupted by user")
        sys.exit(0)
