# MCP Examples

Examples demonstrating MCP (Model Context Protocol) server and client instrumentation with the Observe SDK.

## Prerequisites

You need to set the `OTLP_HTTP_ENDPOINT` variable to point to an OTel collector.
One can deploy one using the docker compose file provided in `deploy/` at the root folder of this repo.

You can set it in a `.env` file:

```bash
cp .env.example .env
# Edit .env with your values
```

Install dependencies:

```bash
pip install mcp ioa-observe-sdk python-dotenv
```

For the LangChain client example, also install:

```bash
pip install langchain-mcp-adapters langgraph langchain-openai
```

## Running the Examples

### 1. Start the MCP Server

```bash
python server.py
```

The server starts on `http://localhost:8000/mcp` using Streamable HTTP transport and exposes `add` and `multiply` tools.

### 2. Run a Client

**Native MCP SDK client** (no LangChain dependency):

```bash
python client_native.py
```

This example uses the MCP SDK's `ClientSession` and `streamablehttp_client` directly to list tools and call them.

**LangChain MCP adapter client** (requires LangChain + OpenAI API key):

```bash
python client.py
```

This example uses `langchain-mcp-adapters` to bridge MCP tools into a LangGraph ReAct agent.

## What Gets Traced

The `McpInstrumentor` automatically instruments all MCP transports (stdio, SSE, Streamable HTTP) and captures:

- **Client-side spans** for each `send_request` call (e.g., `tools/call.mcp`) with input/output attributes
- **Server-side spans** for stream reads/writes with request metadata
- **Cross-process context propagation** via the `_meta` field in MCP requests:
  - W3C `traceparent` and `baggage` for distributed tracing
  - `session.id` for session tracking
  - Agent linking info (`last_agent_span_id`, `last_agent_trace_id`, `last_agent_name`, `agent_sequence`) for span links
  - Fork context (`fork_id`, `fork_parent_seq`, `fork_branch_index`) for parallel execution detection

## Files

| File | Description |
|------|-------------|
| `server.py` | MCP server using FastMCP with `add` and `multiply` tools |
| `client_native.py` | Native MCP SDK client using `ClientSession` directly |
| `client.py` | LangChain MCP adapter client with a ReAct agent |
