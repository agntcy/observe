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

You can run the server app by executing from `remote_agent_slim/app`:

   ```bash
   OTLP_HTTP_ENDPOINT=http://localhost:4318 SLIM_ENDPOINT=http://localhost:46357 python main.py
   ```

### Client

You can run the client app by executing from `remote_agent_slim/client`:

   ```bash
   OTLP_HTTP_ENDPOINT=http://localhost:4318 SLIM_ENDPOINT=http://localhost:46357 python slim.py
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
{"asctime": "2025-03-12 07:13:52,451", "levelname": "INFO", "pathname": "/agntcy/observe/examples/remote_agent_slim/client/slim.py", "module": "slim", "funcName": "main", "message": "", "exc_info": null, "event": "final_result", "result": {"messages": ["content='Write a story about a cat' additional_kwargs={} response_metadata={} id='53de5666-d112-4cbc-969e-21c4477424d7'", "content='Write a story about a cat' additional_kwargs={} response_metadata={} id='2fc19e69-01ef-4b6c-94d2-652b802bc8e5'", "content=\"Once in the quiet town of Willowbrook, nestled between whispering forests and rolling meadows, lived a cat named Whiskers. Whiskers was not an ordinary cat but a brilliant tuxedo cat with a coat of glossy black fur, speckled with flecks of white. His eyes, an enchanting shade of emerald green, seemed to hold the depths of the mysterious forest that bordered the town.\\n\\nWhiskers enjoyed the peaceful life in Willowbrook, spending his days wandering through the quaint cobblestone streets, where he was a beloved figure among the townspeople. But his favorite adventure owed itself to the peculiar routine at the stroke of midnight, when the town was bathed in the silver glow of the moon.\\n\\nOne particular night, the moon hung low and vibrant in the sky, and Whiskers embarked on his nightly exploration. He padded silently along the forest's edge, drawn to the soft murmur of the wind through the treetops and the rustling leaves beneath his paws. As he ventured deeper into the woods, an unfamiliar sound\u2014a soft, melancholic melody\u2014caught his attention.\\n\\nCuriosity piqued, Whiskers followed the enchanting music, which seemed to weave through the trees like a gentle stream. As he emerged into a small forest clearing, he discovered its source: a young girl, seated on a moss-covered log, playing a wooden flute. Her name was Elara, and she had been visiting her grandmother in Willowbrook for the summer.\\n\\nElara\u2019s flute playing was mesmerizing, and Whiskers was entranced. For nights on end, he would return to the clearing, settling quietly by Elara's side as she played her melodies to the moon and stars. In those moments, a friendship blossomed between the girl and the cat, intertwined by the language of music and the unspoken comfort they found in each other's presence.\\n\\nSummer eventually neared its end, and the time came for Elara to leave Willowbrook. On the eve of her departure, she made Whiskers a small charm from twigs and feathers, a token to remember their nights under the moonlit sky. With a heavy heart, she whispered to him, \u201cMay we meet again, under the light of a full moon.\u201d\\n\\nYears passed, and though Whiskers aged into the familiar ways of Willowbrook, he never missed a night under the moon, always hopeful for the soft melodies that had once filled the silent woods.\\n\\nThen, one clear and starlit night many years later, Whiskers, now a respected elder within his feline circles, heard it again\u2014the tender, wistful notes of a flute. With renewed youth in his steps, he followed the sound back to the forest clearing. There was Elara, now grown, her hair kissed by flecks of silver from time, yet her spirit vibrant as the summer they first met.\\n\\nJoyful reunion marked by timeless music, Whiskers and Elara once more shared the concert of their friendship beneath the moon and stars, a testament that some bonds are timeless, and certain memories forever tied to the gentle light of a full moon in the heart of Willowbrook.\" additional_kwargs={} response_metadata={} id='47ec2bf2-e550-4fb9-b138-1f0dd314ce3e'"]}}

```

- Gateway

```bash
2025-03-12T14:13:21.043726Z  INFO data-plane-gateway ThreadId(32) slim_service: running service
2025-03-12T14:13:34.776879Z  INFO data-plane-gateway ThreadId(04) slim_datapath::message_processing: new connection received from remote: (remote: Some(172.17.0.1:49344) - local: Some(172.17.0.2:46357))
2025-03-12T14:13:42.881719Z  INFO data-plane-gateway ThreadId(07) slim_datapath::message_processing: new connection received from remote: (remote: Some(172.17.0.1:46470) - local: Some(172.17.0.2:46357))
2025-03-12T14:13:42.954498Z  INFO data-plane-gateway ThreadId(04) slim_datapath::message_processing: end of stream conn_index=1
```
