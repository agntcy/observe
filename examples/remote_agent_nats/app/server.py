# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import logging
import os

import nats
from dotenv import find_dotenv, load_dotenv

from ioa_observe.sdk import Observe
from ioa_observe.sdk.instrumentations.nats import NATSInstrumentor

from agent.graph import invoke_graph
from logging_config import configure_logging

# Define logger at the module level
logger = logging.getLogger("app")

serviceName = "server-agent"
Observe.init(
    serviceName, api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT", "http://localhost:4318")
)

AGENT_TOPIC = "server"

# Instrument NATS communication
NATSInstrumentor().instrument()


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


# @monitor_error_rates
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
async def serve_agent(uri: str) -> None:
    nc = await nats.connect(uri)

    logger.info(f"Connected to NATS server at {uri}")

    async def handle_request(msg: nats.aio.msg.Msg) -> None:
        logger.info(f"Received message: {msg.data}")
        try:
            payload = json.loads(msg.data.decode("utf8"))
            logger.info(f"Message payload: {payload}")

            reply_msg = process_message(payload)
            await msg.respond(reply_msg.encode())
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode JSON message: {e}")
            error_reply = create_error("Invalid JSON format", 400)
            await nc.publish(msg.reply, error_reply.encode())

        except Exception as e:
            logger.error(f"Error processing message: {e}")
            error_reply = create_error("Internal server error", 500)
            await nc.publish(msg.reply, error_reply.encode())

    subscription = await nc.subscribe(AGENT_TOPIC, cb=handle_request)
    logger.info(f"Subscribed to topic: {AGENT_TOPIC}")

    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        logger.info("Shutting down NATS server...")
        try:
            await subscription.unsubscribe()
            await nc.drain()
            await nc.close()
            logger.info("NATS connection closed.")
        except Exception as e:
            logger.error(f"Error during NATS shutdown: {e}")
        return
    except Exception as e:
        logger.error(f"Error during unsubscribe: {e}")
        return


async def main() -> None:
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
    logger.info("Starting NATS application...")

    load_environment_variables()

    port = os.getenv("PORT", "4222")
    address = os.getenv("NATS_SERVER_ADDR", "127.0.0.1")
    uri = f"nats://{address}:{port}"

    try:
        await serve_agent(uri)
    except KeyboardInterrupt:
        logger.info("Application interrupted")
    except Exception as e:
        logger.info("Unhandled error: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
