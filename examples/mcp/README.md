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
pip install "mcp>=2.2,<3" ioa-observe-sdk python-dotenv
```

For the LangChain client example, also install:

```bash
pip install "langchain[mcp]>=1.4.2" langchain-openai
```

## Running the Examples

### 1. Start the MCP Server

```bash
python server.py
```

The server starts on `http://127.0.0.1:8000/mcp` using Streamable HTTP transport and exposes `add` and `multiply` tools.

### 2. Run a Client

**Native MCP SDK client** (no LangChain dependency):

```bash
python client_native.py
```

This example uses the MCP SDK's high-level `Client` directly to list tools and call them.

**LangChain MCP adapter client** (requires LangChain + OpenAI API key):

```bash
python client.py
```

This example uses LangChain's built-in `langchain.mcp` adapter to bridge MCP tools into a LangChain agent.

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
| `server.py` | MCP 2.x server using `MCPServer` with `add` and `multiply` tools |
| `client_native.py` | Native MCP SDK client using the high-level `Client` |
| `client.py` | LangChain's built-in MCP adapter with a LangChain agent |
