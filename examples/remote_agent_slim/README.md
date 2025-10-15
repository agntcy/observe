# Remote Agents with SLIM

This repository demonstrates an agentic application that communicates with a remote agent with SLIM (Secure Low-Latency Interactive Messaging) Protocol. It has the following simple topology:

```bash
client <-----> SLIM <----> Server
```

- Client contains a Langgraph application
- Server is a FastAPI application that contains the remote agent
- Gateway is a SLIM message broker.

## Requirements

- Python 3.12+
- A virtual environment is recommended for isolating dependencies.
- a `.env` at the proejct root with your OpenAI API key

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
   OTLP_HTTP_ENDPOINT=http://localhost:4318 SLIM_ENDPOINT=http://localhost:46357 python server.py
   ```

### Client

You can run the client app by executing from `client.py`:

   ```bash
   OTLP_HTTP_ENDPOINT=http://localhost:4318 SLIM_ENDPOINT=http://localhost:46357 python client.py
   ```
