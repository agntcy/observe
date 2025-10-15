#!/usr/bin/env python3
"""
SLIM v0.6.0 Server Example

This server demonstrates how to use SLIM v0.6.0 Python bindings to:
1. Create a local SLIM application instance
2. Run a server endpoint for coordination/rendezvous
3. Listen for incoming sessions and handle messages
4. Provide agent capabilities through the observe SDK

Based on SLIM v0.6.0 documentation and examples.
"""

import asyncio
import argparse
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Dict, Any, Optional
import os
import slim_bindings
from openai import AsyncOpenAI
from dotenv import load_dotenv
from ioa_observe.sdk import Observe
from ioa_observe.sdk.connectors.slim import process_slim_msg
from ioa_observe.sdk.instrumentations.slim import SLIMInstrumentor
# from ioa_observe.sdk import observe
from ioa_observe.sdk.decorators import agent

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

serviceName = "remote-server-agent"
Observe.init(serviceName, api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))

SLIMInstrumentor().instrument()

@agent(name="openai_agent", description="OpenAI-powered agent for processing messages")
class OpenAIAgent:
    """
    OpenAI-powered agent for processing messages.
    
    This class handles:
    - OpenAI API integration
    - Response generation with context awareness
    - Error handling and fallback responses
    - Configurable model selection
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gpt-4",
        server_identity: str = "agntcy/example/server"
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model
        self.server_identity = server_identity
        self.client = None
        
        if self.api_key:
            self.client = AsyncOpenAI(api_key=self.api_key)
            logger.info(f"OpenAI agent initialized with model: {self.model}")
        else:
            logger.warning("No OpenAI API key provided. Agent will use echo responses.")
    
    async def generate_response(self, message: str, session_id: str) -> str:
        """
        Generate an AI response using OpenAI.
        
        Args:
            message: The user's message
            session_id: The session identifier for context
            
        Returns:
            AI-generated response
        """
        if not self.client:
            return f"Echo from {self.server_identity} (no AI): {message}"
        
        try:
            # Create a system prompt for the agent
            system_prompt = f"""You are a helpful AI assistant running on a SLIM server.
You are part of session {session_id} and your identity is {self.server_identity}.
Provide helpful, concise responses to user queries.
You can mention that you're running on the SLIM protocol for distributed communication.
Keep responses under 500 tokens and be conversational."""

            # Call OpenAI API
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message}
                ],
                max_tokens=500,
                temperature=0.7,
                timeout=30.0  # Add timeout for reliability
            )
            
            ai_response = response.choices[0].message.content.strip()
            logger.info(f"OpenAI response generated for session {session_id} (model: {self.model})")
            return ai_response
            
        except Exception as e:
            logger.error(f"Error calling OpenAI API: {e}")
            return f"Sorry, I encountered an error processing your request. Echo: {message}"
    
    def is_available(self) -> bool:
        """Check if the OpenAI agent is available."""
        return self.client is not None
    
    def get_model_info(self) -> Dict[str, str]:
        """Get information about the current model configuration."""
        return {
            "model": self.model,
            "available": str(self.is_available()),
            "identity": self.server_identity
        }

class SlimServer:
    """
    SLIM Server using v0.6.0 bindings.
    
    This server:
    - Initializes SLIM with shared secret authentication (dev mode) 
    - Runs a server endpoint for client connections
    - Listens for incoming sessions and processes messages
    - Integrates with the observe SDK for monitoring
    """
    
    def __init__(
        self,
        local_identity: str = "agntcy/example/server",
        endpoint: str = "127.0.0.1:46357",
        shared_secret: str = "secret",
        enable_opentelemetry: bool = False,
        openai_api_key: Optional[str] = None,
        openai_model: str = "gpt-4"
    ):
        self.local_identity = local_identity
        self.endpoint = endpoint
        self.shared_secret = shared_secret
        self.enable_opentelemetry = enable_opentelemetry
        self.slim_app: Optional[slim_bindings.Slim] = None
        self.running = False
        self.active_sessions: Dict[str, Any] = {}
        
        # Initialize OpenAI agent
        self.ai_agent = OpenAIAgent(
            api_key=openai_api_key or os.getenv("OPENAI_API_KEY"),
            model=openai_model,
            server_identity=local_identity
        )
        
    def _split_identity(self, identity: str) -> slim_bindings.PyName:
        """Split identity string into PyName components."""
        try:
            org, namespace, app = identity.split("/")
            return slim_bindings.PyName(org, namespace, app)
        except ValueError as e:
            raise ValueError(f"Identity must be in format 'org/namespace/app', got: {identity}") from e
    
    def _create_identity_providers(self, identity: str, secret: str):
        """Create identity provider and verifier for shared secret auth."""
        provider = slim_bindings.PyIdentityProvider.SharedSecret(
            identity=identity, 
            shared_secret=secret
        )
        verifier = slim_bindings.PyIdentityVerifier.SharedSecret(
            identity=identity, 
            shared_secret=secret
        )
        return provider, verifier
    
    async def initialize(self):
        """Initialize SLIM application and tracing."""
        # Initialize tracing
        await slim_bindings.init_tracing({
            "log_level": "info",
            "opentelemetry": {
                "enabled": self.enable_opentelemetry,
                "grpc": {
                    "endpoint": "http://localhost:4317"
                }
            }
        })
        
        # Create identity components
        provider, verifier = self._create_identity_providers(
            self.local_identity, 
            self.shared_secret
        )
        
        # Convert identity to PyName
        local_name = self._split_identity(self.local_identity)
        
        # Create SLIM application
        self.slim_app = await slim_bindings.Slim.new(local_name, provider, verifier)
        
        logger.info(f"Created SLIM app with ID: {self.slim_app.id_str}")
        logger.info(f"Agent configuration: {self.get_agent_info()}")
        
    async def start_server(self):
        """Start the SLIM server endpoint."""
        if not self.slim_app:
            raise RuntimeError("SLIM app not initialized. Call initialize() first.")
            
        # Start server with insecure TLS for development
        server_config = {
            "endpoint": self.endpoint,
            "tls": {"insecure": True}
        }
        
        await self.slim_app.run_server(server_config)
        logger.info(f"SLIM server running on {self.endpoint}")
        self.running = True
        
    async def stop_server(self):
        """Stop the SLIM server."""
        if self.slim_app and self.running:
            await self.slim_app.stop_server(self.endpoint)
            self.running = False
            logger.info("SLIM server stopped")
    
    # @ai_agent_action(name="process_message")
    async def process_message(self, message: str, session_id: str) -> str:
        """
        Process incoming message with OpenAI agent and observe SDK monitoring.
        
        Args:
            message: The received message
            session_id: The session identifier
            
        Returns:
            AI-generated response message
        """
        logger.info(f"Processing message from session {session_id}: {message}")
        
        # Use OpenAI agent to generate response
        response = await self.ai_agent.generate_response(message, session_id)
        
        # Add some processing delay to simulate work
        await asyncio.sleep(0.1)
        
        return response
    
    def get_agent_info(self) -> Dict[str, Any]:
        """Get information about the current agent configuration."""
        return {
            "server_identity": self.local_identity,
            "agent_model": self.ai_agent.get_model_info(),
            "active_sessions": len(self.active_sessions),
            "running": self.running
        }
    
    async def handle_session(self, session):
        """Handle an incoming session and its messages."""
        session_id = getattr(session, 'id', 'unknown')
        logger.info(f"New session established: {session_id}")
        
        self.active_sessions[session_id] = session
        
        try:
            while True:
                # Wait for incoming message
                msg_ctx, payload = await session.get_message()
                message = payload.decode('utf-8')
                
                # Process message with observability
                response = await self.process_message(message, session_id)
                
                # Send response back
                await session.publish_to(msg_ctx, response.encode('utf-8'))
                logger.info(f"Sent response to session {session_id}")
                
        except Exception as e:
            logger.error(f"Error handling session {session_id}: {e}")
        finally:
            # Clean up session
            if session_id in self.active_sessions:
                del self.active_sessions[session_id]
            logger.info(f"Session {session_id} ended")
    
    async def listen_for_sessions(self):
        """Listen for incoming sessions and handle them concurrently."""
        if not self.slim_app:
            raise RuntimeError("SLIM app not initialized")
            
        logger.info("Listening for incoming sessions...")
        
        while self.running:
            try:
                # Wait for new session
                session = await self.slim_app.listen_for_session()
                
                # Handle session in background task
                asyncio.create_task(self.handle_session(session))
                
            except Exception as e:
                if self.running:  # Only log if we're still supposed to be running
                    logger.error(f"Error listening for sessions: {e}")
                break
    
    async def run(self):
        """Main server run loop."""
        try:
            await self.initialize()
            await self.start_server()
            await self.listen_for_sessions()
        except Exception as e:
            logger.error(f"Server error: {e}")
            raise
        finally:
            await self.stop_server()


async def main():
    """Main entry point with signal handling."""
    parser = argparse.ArgumentParser(description="SLIM v0.6.0 Server Example")
    parser.add_argument(
        "--endpoint", 
        type=str, 
        default="127.0.0.1:46357",
        help="Server endpoint (default: 127.0.0.1:46357)"
    )
    parser.add_argument(
        "--identity",
        type=str,
        default="agntcy/example/server",
        help="Local identity in format org/namespace/app"
    )
    parser.add_argument(
        "--shared-secret",
        type=str,
        default="secret",
        help="Shared secret for authentication (dev only)"
    )
    parser.add_argument(
        "--enable-opentelemetry",
        action="store_true",
        help="Enable OpenTelemetry tracing"
    )
    parser.add_argument(
        "--openai-api-key",
        type=str,
        help="OpenAI API key (or set OPENAI_API_KEY env var)"
    )
    parser.add_argument(
        "--openai-model",
        type=str,
        default="gpt-4",
        help="OpenAI model to use (default: gpt-4)"
    )
    
    args = parser.parse_args()
    
    # Initialize observe SDK
    # observe.configure()
    
    # Create server
    server = SlimServer(
        local_identity=args.identity,
        endpoint=args.endpoint,
        shared_secret=args.shared_secret,
        enable_opentelemetry=args.enable_opentelemetry,
        openai_api_key=args.openai_api_key,
        openai_model=args.openai_model
    )
    
    # Setup signal handling
    stop_event = asyncio.Event()
    
    def signal_handler():
        logger.info("Received shutdown signal")
        stop_event.set()
        server.running = False
    
    # Register signal handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)
    
    # Run server
    server_task = asyncio.create_task(server.run())
    
    try:
        # Wait for either server completion or shutdown signal
        done, pending = await asyncio.wait(
            [server_task, asyncio.create_task(stop_event.wait())],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Cancel remaining tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
                
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    finally:
        logger.info("Server shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer interrupted by user")
        sys.exit(0)