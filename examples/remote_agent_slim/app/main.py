# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import datetime

import slim_bindings
from dotenv import find_dotenv, load_dotenv

from ioa_observe.sdk import Observe
from ioa_observe.sdk.connectors.slim import process_slim_msg
from ioa_observe.sdk.instrumentations.slim import SLIMInstrumentor

from agent.lg import invoke_graph
from core.logging_config import configure_logging

# Define logger at the module level
logger = logging.getLogger("app")

serviceName = "remote-server-agent"
Observe.init(serviceName, api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))


def load_environment_variables(env_file: str | None = None) -> None:
    """
    Load environment variables from a .env file safely.

    This function loads environment variables from a `.env` file, ensuring
    that critical configurations are set before the application starts.

    Args:
        env_file (str | None): Path to a specific `.env` file. If None,
                               it searches for a `.env` file automatically.

    Behavior:
    - If `env_file` is provided, it loads the specified file.
    - If `env_file` is not provided, it attempts to locate a `.env` file in the project directory.
    - Logs a warning if no `.env` file is found.

    Returns:
        None
    """
    env_path = env_file or find_dotenv()

    if env_path:
        load_dotenv(env_path, override=True)
        logging.info(".env file loaded from %s", env_path)
    else:
        logging.warning("No .env file found. Ensure environment variables are set.")


def create_error(error, code) -> str:
    """
    Creates a reply message with an error code
    """
    payload = {
        "message": error,
        "error": code,
    }
    msg = json.dumps(payload)
    return msg


# Helper function to create shared secret identity
def shared_secret_identity(identity, secret):
    """
    Create a provider and verifier using a shared secret.
    """
    provider = slim_bindings.PyIdentityProvider.SharedSecret(
        identity=identity, shared_secret=secret
    )
    verifier = slim_bindings.PyIdentityVerifier.SharedSecret(
        identity=identity, shared_secret=secret
    )
    return provider, verifier


# Helper function to split ID into components
def split_id(id_str):
    """Split an ID into its components"""
    try:
        organization, namespace, app = id_str.split("/")
    except ValueError as e:
        print("Error: IDs must be in the format organization/namespace/app.")
        raise e
    return slim_bindings.PyName(organization, namespace, app)


# @monitor_error_rates
@process_slim_msg("remote_server_agent")
def process_message(payload) -> str:
    """
    Parse the message and looks for errors
    Replies to the incoming message if no error is detected
    """
    logging.debug("Decoded payload: %s", payload)

    # Extract assistant_id from the payload
    agent_id = payload.get("agent_id")
    logging.debug("Agent id: %s", agent_id)

    # Validate that the assistant_id is not empty.
    if not payload.get("agent_id"):
        return create_error("agent_id is required and cannot be empty.", 422)

    # Extract the route from the message payload.
    # This step is done to emulate the behavior of the REST API.
    route = payload.get("route")
    if not payload.get("route") or route != "/runs":
        return create_error("Not Found.", 404)

    message_id = None
    # Validate the config section: ensure that config.tags is a non-empty list.
    if (metadata := payload.get("metadata", None)) is not None:
        message_id = metadata.get("id")

    # -----------------------------------------------
    # Extract the human input content from the payload.
    # We expect the content to be located at: payload["input"]["messages"][0]["content"]
    # -----------------------------------------------

    # Retrieve the 'input' field and ensure it is a dictionary.
    input_field = payload.get("input")
    if not isinstance(input_field, dict):
        return create_error("The 'input' field should be a dictionary.", 500)

    # Retrieve the 'messages' list from the 'input' dictionary.
    messages = input_field.get("messages")
    if not isinstance(messages, list) or not messages:
        return create_error(
            "The 'input.messages' field should be a non-empty list.", 500
        )

    # Access the last message in the list.
    last_message = messages[-1]
    if not isinstance(last_message, dict):
        return create_error(
            "The first element in 'input.messages' should be a dictionary.", 500
        )

    # Extract the 'content' from the first message.
    human_input_content = last_message.get("content")
    if human_input_content is None:
        return create_error(
            "Missing 'content' in the first message of 'input.messages'.", 500
        )

    # We send all messaages to graph
    graph_result = invoke_graph(messages)

    messages = {"messages": graph_result}

    # payload to add to the reply
    payload = {
        "agent_id": agent_id,
        "output": messages,
        "model": "gpt-4o",
        "metadata": {"id": message_id},
    }

    msg = json.dumps(payload)
    return msg


# @monitor_error_rates
# @log_connection_events
# @measure_connection_latency
async def connect_to_gateway(address, enable_opentelemetry=False) -> tuple[str, str]:
    """
    Connects to the remote gateway, subscribes to messages, and processes them.

    Returns:
        A tuple containing:
        - The source agent (str) that sent the last received message.
        - The last decoded message (dict).
    """

    # An agent app is identified by a name in the format
    # /organization/namespace/agent_class/agent_id. The agent_class indicates the
    # type of agent, and there can be multiple instances of the same type running
    # (e.g., horizontal scaling of the same app in Kubernetes). The agent_id
    # identifies a specific instance of the agent and it is returned by the
    # create_agent function is not provided
    local_id = "cisco/default/server"

    # Use shared secret for authentication (for demo purposes)
    shared_secret = os.getenv("SLIM_SHARED_SECRET", "demo-secret")

    # init tracing
    await slim_bindings.init_tracing(
        {"log_level": "info", "opentelemetry": {"enabled": enable_opentelemetry}}
    )

    # Create identity provider and verifier
    provider, verifier = shared_secret_identity(local_id, shared_secret)

    # Split the local ID into components
    local_name = split_id(local_id)

    # Create participant with new API
    participant = await slim_bindings.Slim.new(local_name, provider, verifier)

    # Connect to gateway server
    connection_config = {"endpoint": address, "tls": {"insecure": True}}
    _ = await participant.connect(connection_config)

    # # Subscribe to receive messages
    # await participant.subscribe(local_name)

    logger.info(f"Connected to SLIM gateway at {address}")

    # Initialize SLIM connector
    # slim_connector = SLIMConnector(
    #     remote_org="cisco",
    #     remote_namespace="default",
    #     shared_space="chat",
    # )
    #
    # # Register agents with the connector
    # slim_connector.register("remote_server_agent")

    # # Instrument SLIM communications
    SLIMInstrumentor().instrument()

    last_src = ""
    last_msg = ""

    try:
        logger.info(
            "SLIM server started for agent: %s",
            local_id,
        )
        async with participant:
            # Create our own session for listening to messages
            # server_session = await participant.create_session(
            # slim_bindings.PySessionConfiguration.FireAndForget(
            #     max_retries=5,
            #     timeout=datetime.timedelta(seconds=30),
            #     mls_enabled=False,
            # )
            # )
            # logger.info(f"Server created session: {server_session.id}")

            while True:
                try:
                    # Wait for messages from any session
                    logger.info("Waiting for messages...")
                    session_info, message = await participant.receive()

                    if message is None:
                        logger.info(
                            "Received empty message (session establishment), continuing..."
                        )
                        continue

                    # Process the message
                    logger.info(f"Received message from session {session_info.id}")

                    try:
                        payload = json.loads(message.decode("utf8"))
                        logger.info(f"Message payload: {payload}")

                        reply_msg = process_message(payload)
                        logger.info(f"Sending reply: {reply_msg}")
                        # receiver = slim_bindings.PyName("cisco", "default", "client")

                        # await participant.set_route(receiver)

                        await participant.publish_to(session_info, reply_msg.encode())

                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to decode JSON message: {e}")
                        error_reply = create_error("Invalid JSON format", 400)
                        client_name = split_id("cisco/default/client")
                        await participant.publish_to(session_info, error_reply.encode())

                    except Exception as e:
                        logger.error(f"Error processing message: {e}")
                        error_reply = create_error("Internal server error", 500)
                        client_name = split_id("cisco/default/client")
                        await participant.publish_to(session_info, error_reply.encode())

                except Exception as e:
                    logger.error(f"Error in main receive loop: {e}")
                    await asyncio.sleep(1)  # Brief pause before retrying

    except asyncio.CancelledError:
        print("Shutdown server")
        raise
    finally:
        print(f"Shutting down agent {local_id}")
        return last_src, last_msg  # Return last received source and message


# @monitor_error_rates
# @measure_chain_completion_time
async def try_connect_to_gateway(address, port, max_duration=300, initial_delay=1):
    """
    Attempts to connect to a gateway at the specified address and port using exponential backoff.
    This asynchronous function repeatedly tries to establish a connection by calling the
    connect_to_gateway function. If a connection attempt fails, it logs a warning and waits for a period
    that doubles after each failure (capped at 30 seconds) until a successful connection is made or until
    the accumulated time exceeds max_duration.
    Parameters:
        address (str): The hostname or IP address of the gateway.
        port (int): The port number to connect to.
        max_duration (int, optional): Maximum duration (in seconds) to attempt the connection. Default is 300.
        initial_delay (int, optional): Initial delay (in seconds) before the first retry. Default is 1.
    Returns:
        tuple: Returns a tuple containing the source and a message received upon successful connection.
    Raises:
        TimeoutError: If the connection is not successfully established within max_duration seconds.
    """
    start_time = time.time()
    delay = initial_delay

    while time.time() - start_time < max_duration:
        try:
            src, msg = await connect_to_gateway(f"http://{address}:{port}")
            return src, msg
        except Exception as e:
            logger.warning(
                "Connection attempt failed: %s. Retrying in %s seconds...", e, delay
            )
            await asyncio.sleep(delay)
            delay = min(
                delay * 2, 30
            )  # Exponential backoff, max delay capped at 30 sec

    raise TimeoutError("Failed to connect within the allowed time frame")


def main() -> None:
    """
    Entry point for running the application.

    This function performs the following:
    - Configures logging globally.
    - Loads environment variables from a `.env` file.
    - Retrieves the address for the remote gateway
    - Connects to the gateway and waits for incoming messages

    Returns:
        None
    """
    configure_logging()
    logger.info("Starting SLIM application...")

    load_environment_variables()

    port = os.getenv("PORT", "46357")
    address = os.getenv("SLIM_ADDRESS", "127.0.0.1")

    try:
        # session_start()  # Initialize observability session
        src, msg = asyncio.run(try_connect_to_gateway(address, port))
        logger.info("Last message received from: %s", src)
        logger.info("Last message content: %s", msg)
    except KeyboardInterrupt:
        logger.info("Application interrupted")
    except Exception as e:
        logger.info("Unhandled error: %s", e)


if __name__ == "__main__":
    main()
