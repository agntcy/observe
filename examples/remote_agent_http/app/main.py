# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import os
from datetime import datetime

from dotenv import find_dotenv, load_dotenv
from flask import Flask, request, jsonify

from agent.lg import invoke_graph
from core.logging_config import configure_logging
from ioa_observe.sdk.tracing.context_utils import set_context_from_headers


# Flask app
app = Flask(__name__)
logger = logging.getLogger("app")


def load_environment_variables(env_file: str | None = None) -> None:
    env_path = env_file or find_dotenv()
    if env_path:
        load_dotenv(env_path, override=True)
        logger.info(".env file loaded from %s", env_path)
    else:
        logger.warning("No .env file found. Ensure environment variables are set.")


def create_error(message, code):
    response = {"message": message, "error": code}
    return jsonify(response), code


@app.route("/runs", methods=["POST"])
def process_message():
    """
    Emulates SLIM message processing over HTTP.
    """
    try:
        payload = request.get_json()
        # get headers
        headers = request.headers
        if headers:
            set_context_from_headers(headers)
        logger.debug("Received payload: %s", payload)

        agent_id = payload.get("agent_id")
        if not agent_id:
            return create_error("agent_id is required and cannot be empty.", 422)

        message_id = None
        if metadata := payload.get("metadata"):
            message_id = metadata.get("id")

        input_field = payload.get("input")
        if not isinstance(input_field, dict):
            return create_error("The 'input' field should be a dictionary.", 500)

        messages = input_field.get("messages")
        if not isinstance(messages, list) or not messages:
            return create_error("The 'input.messages' field should be a non-empty list.", 500)

        last_message = messages[-1]
        if not isinstance(last_message, dict):
            return create_error("Each message should be a dictionary.", 500)

        human_input_content = last_message.get("content")
        if human_input_content is None:
            return create_error("Missing 'content' in the last message of 'input.messages'.", 500)

        graph_result = invoke_graph(messages)

        response_payload = {
            "agent_id": agent_id,
            "output": {"messages": graph_result},
            "model": "gpt-4o",
            "metadata": {"id": message_id},
        }

        return jsonify(response_payload)

    except Exception as e:
        logger.exception("Unexpected server error")
        return create_error(f"Internal Server Error: {str(e)}", 500)


def main():
    configure_logging()
    logger.info("Starting HTTP server...")

    load_environment_variables()

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 5000))

    app.run(host=host, port=port)


if __name__ == "__main__":
    main()
