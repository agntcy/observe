# Real-Time Observability — Phase 4 Demo

A self-contained, one-command demo of the real-time observability add-on.

It runs a small multi-agent workflow (`planner → researcher → writer →
synthesizer`, with a `web_search` tool) and renders the **live `SessionState`**
as runtime events are pushed during execution — before the corresponding spans
flush.

## What it shows

1. **Pushed runtime events** — each event prints as it is emitted while the
   session is still running.
2. **Live agent graph** — nodes move from `active` to `done`, and handoff edges
   appear the moment an agent hands off to the next.
3. **Tool visibility** — the `web_search` tool shows as `running` while in
   flight, independent of the final spans.
4. **Live-before-spans** — the materialized state is built from runtime events
   during execution; spans for the same run are still batched and would only
   reach a backend ~5s later.

## Run it

```bash
cd examples/realtime_demo
./run.sh
# or
uv run --project ../.. python agents.py
```

No external OTel Collector or ClickHouse is required. The demo attaches the
`SessionStateMaterializer` in-process via the runtime-event listener so it runs
with a single command.

## Third-party OTel SDK mapping example

`third_party_otel.py` creates and owns its own `TracerProvider`, instruments
agent, model, and tool spans using current `gen_ai.*` semantic-convention
attributes, and sends traces and mapped runtime-event logs to an OTLP/HTTP
collector:

```bash
uv run python examples/realtime_demo/third_party_otel.py
```

Start an OTLP/HTTP collector on port `4318` before running this example.
The endpoint defaults to `http://localhost:4318` and honors
`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`, and
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`.

The application keeps ownership of its provider and existing span processor.
Observe's processor owns the default GenAI mapper and private Logs pipeline:

```python
provider.add_span_processor(
    RuntimeEventSpanProcessor(
        endpoint="http://localhost:4318/v1/logs",
        resource=resource,
    )
)
```

## How it maps to production

In production the materializer lives **downstream** and consumes the same OTel
runtime events from the telemetry pipeline (see
`ioa_observe/materializer/clickhouse.py` for the ClickHouse consumer, and
`docs/REALTIME_OBSERVABILITY_POC_PLAN.md` for the full data flow). This demo
swaps the transport for an in-process listener purely so it is runnable
locally with no backend.
