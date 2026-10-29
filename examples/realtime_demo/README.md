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

The default mapper reads `gen_ai.conversation.id` as the session ID and maps
`invoke_agent`, `execute_tool`, `chat`, `generate_content`, and `text_completion`
spans to runtime events. Agent `gen_ai.input.messages` becomes `agent.input`
on both start and completion events when available; `gen_ai.output.messages`
becomes `agent.output` on completion. A non-empty `gen_ai.agent.name` on an
agent, model, or tool span becomes `agent.name`. Tool events also retain the
existing `gen_ai.agent.name` attribute for compatibility.

Message content is forwarded only when the source instrumentation records it;
the processor does not enable content capture or infer agent names from parent
spans. Attributes needed for start events must be supplied at span creation.
Attributes added later are available only on completion events. Protocol and
capability events, session lifecycle events, and topology edges are not inferred
by this mapper.

## Vendor SDKs (Traceloop, OpenInference, Langfuse)

`RuntimeEventSpanProcessor` first runs span normalizers
(`ioa_observe/sdk/tracing/span_normalizers.py`) that add canonical `gen_ai.*`
attributes derived from vendor-specific ones. Existing `gen_ai.*` values are never
overwritten.

| SDK | Detected by | Mapped from |
|---|---|---|
| Traceloop / OpenLLMetry | `traceloop.span.kind`, `llm.request.type` | `traceloop.entity.*`, `traceloop.association.properties.session_id`, `gen_ai.prompt.N.*` |
| OpenInference | `openinference.span.kind` | `session.id`, `tool.name`, `input.value`/`output.value`, `llm.*` |
| Langfuse v3+ | `langfuse.observation.type` | `langfuse.session.id`, `langfuse.observation.*` |

A session id is also taken from `session.id`, `thread_id` or `conversation_id`
when `gen_ai.conversation.id` is absent. Model spans without a provider get
`gen_ai.provider.name="unknown"`. Pass `normalizers=()` to disable this, or your
own sequence to extend it.

### Derived events

Third-party SDKs never call Observe's topology helpers, so the processor also
infers (disable with `derive_events=False`):

| Event | Inferred from |
|---|---|
| `topology.session.started` / `.completed` | first span seen for a session / the session's last open span ending (a later span starts a new session cycle) |
| `topology.edge.updated` | an `invoke_agent` span nested under a different agent's span (`evidence=parent_span`, `confidence=0.8`) |
| `llm.tools.presented` | `gen_ai.tool.definitions` on a model span (honors content-tracing and size limits) |

For a run against the real Traceloop SDK (OpenAI mocked), see
`traceloop_real.py` (`pip install traceloop-sdk openai httpx`). Run the offline example (no collector needed):

```bash
uv run python examples/realtime_demo/vendor_sdks_otel.py
```

## Limitations with third-party SDKs

Native Observe instrumentation emits richer events than can be derived from
third-party spans. What is missing or not possible:

**Not produced at all**

| Event | Why |
|---|---|
| `mcp.tools.discovered`, `a2a.agent_card` | Emitted only from Observe's MCP/A2A instrumentation; no standard span attributes carry these. |
| `mcp.message.*`, `a2a.message.*`, `slim.message.*` | Transport-level events from Observe's own instrumentations. |
| Retriever, embedding, chain, guardrail and evaluator events | Only `invoke_agent`, `execute_tool` and `chat`/`generate_content`/`text_completion` operations are mapped; other spans are ignored. |
| Span-error status | Failed spans are not reported as failed (`tool.completed` is only marked `error` from the tool result payload). |

**Produced, but degraded**

- **Session required.** Spans with no session id (`gen_ai.conversation.id`,
  `session.id`, `thread_id`, `conversation_id`, Traceloop `session_id`,
  `langfuse.session.id`) are dropped. It must be set before the span starts
  (e.g. call `Traceloop.set_association_properties` before the workflow).
- **Start events depend on the SDK.** Real-time `*.started` events can only be
  mapped from attributes present when the span is created. Measured with real
  SDKs, with `backfill_start_events=False`:

  | SDK | `topology.node.started` | `tool.started` | `llm.started` |
  |---|---|---|---|
  | Traceloop 0.62.4 | no | no | no |
  | OpenInference (openai 0.1.66) | yes | yes | no (model set after start) |
  | Langfuse 5.0.4 | no | no | no |

  By default the processor **backfills** the missing start event when the span
  ends (`backfill_start_events=True`), timestamped with the span's real start
  time. Consumers get consistent start/complete pairs, but the start is
  delivered at completion, so it is not real-time. Pass
  `backfill_start_events=False` to only emit true start events.
- **Inferred, not observed.** `topology.session.*` and `topology.edge.updated`
  come from span structure (`evidence=parent_span`, `confidence=0.8`). Edges
  from cross-process handoffs, MCP/A2A/SLIM transports or non-nested agents
  (`message.id`, `fork.id`, `sequence`) cannot be inferred. A session
  completes when its last open span ends, so a second top-level span starts a
  new session cycle, and spans in other processes are not seen.
- **Edges are emitted once per agent pair per session**, and for SDKs without
  start-time agent names, only after the parent agent span ends.
- **Provider is guessed.** Langfuse and similar SDKs omit the provider, so model
  events carry `gen_ai.provider.name="unknown"`.
- **`llm.tools.presented`** requires `gen_ai.tool.definitions` on the model
  span, which many SDKs do not set. Content and size limits follow
  `OBSERVE_CAPABILITY_MAX_PAYLOAD_BYTES` and content tracing settings.
- **Attribute coverage.** Vendor mappings were checked against recorded spans
  from Traceloop 0.62.4, openinference-instrumentation-openai 0.1.66 and
  Langfuse 5.0.4; other versions and the legacy `gen_ai.prompt.N.*` path are
  untested.

## How it maps to production

In production the materializer lives **downstream** and consumes the same OTel
runtime events from the telemetry pipeline (see
`ioa_observe/materializer/clickhouse.py` for the ClickHouse consumer, and
`docs/REALTIME_OBSERVABILITY_POC_PLAN.md` for the full data flow). This demo
swaps the transport for an in-process listener purely so it is runnable
locally with no backend.
