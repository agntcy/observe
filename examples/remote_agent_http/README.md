# Remote Agents with HTTP

This repository demonstrates an agentic application that communicates with a remote agent with HTTP Protocol. It has the following simple topology:

```bash
client <----> Server
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
   cd observe/examples/remote_agent_http
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

### Server

You can run the server app by executing from `remote_agent_http/app`:

   ```bash
   OTLP_HTTP_ENDPOINT=http://localhost:4318 python main.py
   ```

### Client

You can run the client app by executing from `remote_agent_http/client`:

   ```bash
   OTLP_HTTP_ENDPOINT=http://localhost:4318 python client.py
   ```

### Output

On a successful run you should an output similar to the following:

- client:

```bash
{"timestamp": "2025-03-12 07:13:42,912", "level": "INFO", "message": "{'event': 'final_result', 'result': {'messages': [HumanMessage(content='Write a story about a cat', additional_kwargs={}, response_metadata={}, id='c97f93dd-0c55-4109-862b-a34d6fd5aeba'), AIMessage(content='cats are wise', additional_kwargs={}, response_metadata={}, id='c79d1515-340e-43b6-b16c-9e04ae2c3058')]}}", "module": "slim", "function": "<module>", "line": 212, "logger": "graph_client", "pid": 20472}
```

- server:

```bash
{"timestamp": "2025-03-12 07:13:42,910", "level": "INFO", "message": "Received message{\"agent_id\": \"remote_agent\", \"output\": {\"messages\": [{\"role\": \"assistant\", \"content\": \"cats are wise\"}]}, \"model\": \"gpt-4o\", \"metadata\": {\"id\": \"d90cafe8-8f0c-4012-937f-df98356262cc\"}}, from agent <builtins.PyAgentSource object at 0x0000020A5BA2A5F0>", "module": "main", "function": "connect_to_gateway", "line": 184, "logger": "app", "pid": 5808}
```

- client:

```bash
{"asctime": "2025-03-12 07:13:52,952", "levelname": "INFO", "pathname": "/agntcy/observe/examples/remote_agent_http/client/client.py", "module": "client", "funcName": "main", "message": "", "exc_info": null, "event": "final_result", "result": {"messages": ["content='Write a story about a cat' additional_kwargs={} response_metadata={} id='18a654f4-f206-40ab-9b53-dfa98d864a35'", "content='Write a story about a cat' additional_kwargs={} response_metadata={} id='d966a15f-73c0-480b-8554-a240c0d5b1bb'", "content='Once upon a time, in the bustling city of Meowpolis, lived a curious and adventurous feline named Oliver. Oliver was not an ordinary cat; he had an insatiable curiosity about the world beyond his cozy apartment, where he lived with his human, Emily.\\n\\nEvery day, Oliver would gaze out the window, watching the world go by. The bustling streets below were filled with people rushing to and fro, tall buildings reaching towards the sky, and vibrant market stalls that seemed to promise adventures aplenty. Despite Emily\\'s love and attention, Oliver\\'s heart yearned for exploration and the thrill of the unknown.\\n\\nOne crisp autumn morning, as Emily prepared to head to work, she accidentally left the window slightly ajar. Sensing an opportunity, Oliver leaped onto the windowsill. A gust of cool wind ruffled his fur, and he felt a flicker of excitement. With a graceful leap, he jumped out and landed on the fire escape.\\n\\nOutside, the air was filled with new scents and sounds. Excited yet cautious, Oliver made his way down the metal staircase to the alley below. The city unveiled itself before him in all its glory. Vibrant taxis zoomed past, pigeons cooed from the rooftops, and the aroma of freshly baked bread wafted from a nearby bakery.\\n\\nDetermined to make the most of his adventure, Oliver began to explore the winding streets. He marveled at the street performers in the square and chuckled as children chased a stray balloon. A friendly street vendor even offered him a small piece of fish, which he gratefully accepted.\\n\\nAs the day wore on, Oliver found himself at the heart of Meowpolis\\'s bustling market. Stalls brimmed with colorful fruits, vegetables, and delightful trinkets. In the center of the square, a large fountain bubbled joyfully, where he paused to take a drink and rest his paws.\\n\\nJust as Oliver was about to continue his exploration, he heard a familiar voice calling his name. It was Emily! She had returned home to find him missing and had launched a search through the city, desperate to find her beloved cat. Oliver\\'s heart leapt with joy as he raced towards her open arms.\\n\\nEmily scooped him up, relief flooding her face. \"Oh, Oliver, you had me so worried!\" she exclaimed, hugging him tightly. Despite his grand adventure, Oliver realized that there was no place like home, and no one like Emily to share his adventures with.\\n\\nFrom that day forward, Emily often took Oliver on gentle walks through the city, safely harnessed, allowing him to satisfy his curiosity under her watchful eye. And though Oliver still had a penchant for adventure, he knew he would always have a loving home to return to, where he could share tales of the city with Emily as she petted him to sleep.\\n\\nAnd so, Oliver continued to explore, discover, and dream, writing the chapters of his story in the vibrant city of Meowpolis, with Emily always by his side.' additional_kwargs={} response_metadata={} id='e78f6aad-49d7-4fda-bb1d-f49adb1e3696'"], "session_id": {"executionID": "remote-client-agent_http_17038049-5714-4c6c-ab14-4d27ec20872f", "traceparentID": "00-8b5adbbda7baa4f3bfcac754ba3a3de4-9b2ce575d6e1ee4c-01"}}}
```