# Remote Agents with SLIM

This repository demonstrates an agentic application that communicates with a remote agent with SLIM (Secure Low-Latency Interactive Messaging) Protocol v1.x. It has the following simple topology:

```bash
client <-----> SLIM <----> Server
```

- Client creates sessions and sends messages to the server
- Server listens for sessions and responds using an OpenAI agent
- Gateway is a SLIM message broker

## SLIM v1.x API

This example uses the SLIM v1.x API with the following key features:

- **Service-based architecture**: `slim_bindings.Service` + `slim_bindings.App` pattern
- **Initialization**: `initialize_with_configs()` with config objects
- **App creation**: `App.new_with_secret(name, shared_secret)` for shared secret auth
- **Session API**: `create_session_and_wait_async()` returns a session, `get_message_async()` returns `ReceivedMessage`
- **Session properties are methods**: `session.session_id()`, `session.source()`, `session.destination()`
- **Publish API**: `session.publish_async(data, topic, metadata)` requires additional parameters

## Requirements

- Python 3.12+
- slim-bindings >= 1.0.0
- A virtual environment is recommended for isolating dependencies
- A `.env` at the project root with your OpenAI API key

## Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/agntcy/observe
   cd observe/examples/remote_agent_slim
   ```

2. Install the required dependencies:

   ```bash
   set -a
   source .env
   set +a

   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Ensure you have an Otel collector running. You can use the provided `otel-collector.yaml` file to set up a local collector with Docker:

   ```bash
   cd observe/deploy
   docker compose up -d
   ```


## Running the Application

### SLIM Gateway

The preferred method to run the SLIM remote agent is Docker.

   ```bash
   docker compose up -d
   ```

### Server

You can run the server app by executing from `server.py`:

   ```bash
   OTLP_HTTP_ENDPOINT=http://localhost:4318 python server.py
   ```

### Client

You can run the client app by executing from `client.py`:

   ```bash
   OTLP_HTTP_ENDPOINT=http://localhost:4318 python client.py
   ```
