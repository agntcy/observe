# Observe-SDK

IOA observability SDK for your multi-agentic application.

## Table of Contents

- [Installation](#installation)
- [Usage](#usage)
- [Features](#features)
- [Contributing](#contributing)

## Installation

To install the package via PyPI, simply run:

```bash
pip install ioa_observe_sdk
```

Alternatively, to download the SDK from git, you could also use the following command. Ensure you have `uv` installed in your environment.

```bash
uv add "git+https://github.com/agntcy/observe"
```

## Dev

To get started with development, start a Clickhouse DB and an OTel collector container locally using docker-compose like so:

```
cd deploy/
docker compose up -d
```

Ensure the contents of `otel-collector.yaml` is correct.

Check the logs of the collector to ensure it is running correctly:

```
docker logs -f otel-collector
```

Create a `.env` file with the following content:

```bash
OTLP_HTTP_ENDPOINT=http://localhost:4318
```

Install the dependencies and activate the virtual environment:

```bash
set -a
source .env
set +a

python3 -m venv .venv
source .venv/bin/activate
uv sync
```

## Testing

To run the unit tests, ensure you have the `OPENAI_API_KEY` set in your environment. You can run the tests using the following command:

```bash
OPENAI_API_KEY=<KEY> make test
```

## Getting Started

For getting started with the SDK, please refer to the [Getting Started](https://github.com/agntcy/observe/blob/main/GETTING-STARTED.md)
 file. It contains detailed instructions on how to set up and use the SDK effectively.

## SLIM-Based Multi-Agentic Systems

For distributed agent systems using SLIM protocol, additional instrumentation is available:


### Initializing the SLIM Connector with your agent

```python
from ioa_observe.sdk.connectors.slim import SLIMConnector, process_slim_msg
from ioa_observe.sdk.instrumentations.slim import SLIMInstrumentor

# Initialize SLIM connector
slim_connector = SLIMConnector(
    remote_org="cisco",
    remote_namespace="default",
    shared_space="chat",
)

# Register agents with the connector
slim_connector.register("remote_client_agent")

# Instrument SLIM communications
SLIMInstrumentor().instrument()
```

### Receiving Messages with a Callback

Add the decorator `process_slim_msg` to the callback function to process incoming messages. This function will be called whenever a message is received in the shared space.

```python

# Define a callback to process incoming messages
from ioa_observe.sdk.connectors.slim import SLIMConnector, process_slim_msg
import json
from typing import Dict, Any

@process_slim_msg("remote_client_agent")
async def send_and_recv(msg) -> Dict[str, Any]:
    """Send message to remote endpoint and wait for reply."""
    gateway = GatewayHolder.gateway
    session_info = GatewayHolder.session_info

    if gateway is not None:
        await gateway.publish(session_info, msg.encode(), "cisco", "default", "server")
        async with gateway:
            _, recv = await gateway.receive(session=session_info.id)
    else:
        raise RuntimeError("Gateway is not initialized yet!")

    response_data = json.loads(recv.decode("utf8"))
    return {"messages": response_data.get("messages", [])}
```

### Starting the Message Receiver

```python
# Start receiving messages from the SLIM shared space
await slim.receive(callback=on_message_received)
```

### Publishing Messages

```python
# Publish a message to the SLIM shared space
message = {"type": "ChatMessage", "author": "moderator", "message": "Hello, world!"}
await slim.publish(msg=json.dumps(message).encode("utf-8"))
```

We will observe various events and metrics being sent to the Otel collector as we interact with other agents in the shared space via SLIM.

## Contributing

Contributions are welcome! Please follow these steps to contribute:

1. Fork the repository.
2. Create a new branch (`git checkout -b feature-branch`).
3. Commit your changes (`git commit -am 'Add new feature'`).
4. Push to the branch (`git push origin feature-branch`).
5. Create a new Pull Request.
