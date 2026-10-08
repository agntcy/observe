# Running the A2A 1.0 examples

These examples use the A2A 1.0 protocol. The HTTP examples use `a2a-sdk`
1.1.x APIs. The SLIM examples use `slima2a` 0.7.0 and its pinned compatible
`a2a-sdk` 1.1.0 dependency.

You need to set the `OTLP_HTTP_ENDPOINT` variable to point to an otel collector.
One can deploy one using the docker compose file provided in `deploy/` at the root folder of this repo.

You can set it in a `.env` file:
```
$ cat <<< EOF > .env
OTLP_HTTP_ENDPOINT="http://localhost:4318"
EOF
```

Install the locked environment:

`uv sync`

For integration scope, current gaps, and acceptance evidence, see the
[Phase 1 event subscription and span correlation plan](EVENT_STREAM_POC_PLAN.md)
and the [Phase 2 A2A Enforcement and Cognition Store plan](A2A_ENFORCEMENT_POC_PLAN.md).

## Plain A2A example

Start the server:

`uv run --env-file .env a2aserver`

In another terminal, run the client:

`uv run --env-file .env a2aclient`

### Inspecting request metadata

The clients print request metadata from an A2A interceptor, after Observe's
BaseClient injection and before calling the transport. Each entry
includes the operation (`send_message` or `send_message_streaming`), the full
metadata, `metadata.observe.traceparent`, and its decoded `trace_id` and
`span_id`. `metadata_location` identifies the message or outer request metadata.
With the pinned SDK and current Observe injection, the value lives at
`message.metadata.observe.traceparent`. The server prints the received metadata
before executing the agent, labelled `agent.execute`. Streaming responses do not
each produce a new request metadata entry.

The IDs are decoded from the propagated value, not from the currently active
span. Missing or invalid trace context is explicitly labelled and its decoded
IDs are printed as `null`. These diagnostics do not change metadata or tracing
behavior. The hello-world HTTP and SLIM examples print the same diagnostics.
SLIM transport instrumentation can inject metadata again after the client
interceptor runs; the server print shows the received value.

Compare the outgoing and incoming `traceparent` values, then compare their IDs
with the `trace.id` and `span.id` attributes on A2A runtime events. Equality with
the send event is not guaranteed on every instrumentation path, particularly
when stored incoming trace context is reused.

These examples print full request metadata, including baggage. Use only demo
data and avoid exposing sensitive metadata in shared logs.

## A2A over SLIM example

You need to have an instance of the SLIM gateway running locally to have this working:

```
$ cd examples/remote_agent_slim/
$ docker compose up
```

Start the server:

`uv run --env-file .env slima2aserver`

In another terminal, run the client:

`uv run --env-file .env slima2aclient`
