# Remote Agents with NATS

This repository demonstrates an agentic application that communicates with a remote agent with NATS pubsub. It has the following simple topology:

```bash
client.publish <-----> NATS-server <----> Server.subscribe
```

- Client contains a Langgraph application
- Server is a FastAPI application that contains the remote agent

## Requirements

- Python 3.12+
- A virtual environment is recommended for isolating dependencies.
- a `.env` at the proejct root with your OpenAI API key

## Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/agntcy/observe
   cd observe/examples/remote_agent_nats
   ```

2. Install the required dependencies:

   ```bash
   set -a
   source .env
   set +a

   uv venv
   source .venv/bin/activate
   uv sync
   ```

   Optionally install observe-sdk from source:
   ```bash
   make install-dev
   ```

## Running the Application

### Nats and observability service stack

To run a nats server, otel collector, clickhouse-db, and grafana, use the provided `docker-compose.yaml`.

   ```bash
   docker-compose up -d
   ```

### Server

You can run the server using the provided Makefile or running `app/server.py` directly.

   ```bash
   make run-server
   ```

### Client

You can run the client the provided Makefile or running `client/client_agent.py` directly.

   ```bash
   make run-client
   ```

### Output

On a successful run you should an output similar to the following:

- client:

```bash
INFO:client_agent:Connected to NATS server at 127.0.0.1
INFO:client_agent:{'event': 'invoking_graph', 'inputs': {'messages': [HumanMessage(content='Write a very short story about a cat', additional_kwargs={}, response_metadata={})]}}
INFO:client_agent:{"event": "sending_request", "query": "Write a very short story about a cat"}
```

- server:

```bash
{"timestamp": "2025-10-20 08:19:53,534", "level": "INFO", "message": "Logging is initialized. This should appear in the log file.", "module": "logging_config", "function": "configure_logging", "line": 144, "logger": "app", "pid": 23271}
{"timestamp": "2025-10-20 08:19:53,534", "level": "INFO", "message": "Starting NATS application...", "module": "server", "function": "main", "line": 204, "logger": "app", "pid": 23271}
{"timestamp": "2025-10-20 08:19:53,539", "level": "INFO", "message": "Connected to NATS server at nats://127.0.0.1:4222", "module": "server", "function": "serve_agent", "line": 150, "logger": "app", "pid": 23271}
{"timestamp": "2025-10-20 08:19:53,539", "level": "INFO", "message": "Subscribed to topic: server", "module": "server", "function": "serve_agent", "line": 171, "logger": "app", "pid": 23271}
```

- client:

```bash
INFO:client_agent:{'event': 'final_result', 'result': {'messages': [HumanMessage(content='Write a very short story about a cat', additional_kwargs={}, response_metadata={}, id='9ab7430d-5d05-4e3e-9489-0051322332e1'), HumanMessage(content='Write a very short story about a cat', additional_kwargs={}, response_metadata={}, id='24f5fc98-9ced-4c67-bcee-5e100c9a050e'), AIMessage(content='Whiskers, a curious calico cat, loved exploring the little garden behind her house. Each morning, she would awaken with the sun, stretching luxuriously before prancing out through the open backdoor. Today, as she padded over the dew-kissed grass, she discovered something extraordinary—a tiny blue butterfly fluttering around a cluster of daisies. Mesmerized, Whiskers pounced gently, not to capture, but to frolic alongside her delicate new friend. Together, they danced under the warming light, creating a fleeting, joyful bond that made that morning truly magical. As the butterfly eventually fluttered away, Whiskers watched it disappear into the sky, her heart light and content with the beauty she had experienced.', additional_kwargs={}, response_metadata={}, id='355e05a6-f8cf-47ed-8ae3-40bebc15dfc0')]}}
```