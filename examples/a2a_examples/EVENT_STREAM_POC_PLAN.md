# Phase 1 PoC: CE Event Subscriptions and Offline Span Correlation

## Objective

Demonstrate that CASA and Security Detection can subscribe to OXP's realtime
event stream, receive agreed events at their configured POST callbacks, and
correlate those events with completed spans through the OXP Analytics API.

This plan separates behavior provided by Observe from integration behavior
that must be demonstrated in the deployed event exporter, OXP, and CEs. Those
external services, their subscription APIs, callback implementation, and
Analytics API contract are not supplied by these examples.

Phase 1 establishes event delivery and correlation.
[Phase 2](A2A_ENFORCEMENT_POC_PLAN.md) establishes in-band A2A enforcement and
Cognition Store persistence. Receiving an event in Phase 1 does not block,
authorize, or modify an agent action.

## Target flow

```text
Sample application
  -> runtime-event OTel logs -> Collector -> event exporter / OXP subscriptions
                                             -> CASA POST callback
                                             -> Security Detection POST callback
  -> completed OTel spans -> Collector -> span store / OXP Analytics API
                                             ^
                                             |
                          CE queries by event trace/span IDs
```

The application can export to the Collector over OTLP/HTTP. The required
Collector-to-event-exporter hop is OTLP logs over gRPC. These are separate
connections with independently configured endpoints.

## What is available today

| Capability | Current behavior |
|---|---|
| Observe runtime events | Session, agent, topology, LLM, tool, A2A, SLIM, and MCP event families, subject to the relevant instrumentation and configuration. |
| Log correlation | The emitter copies the supplied/current span's IDs into the OTel LogRecord and matching `trace.id` / `span.id` attributes. The span-lifecycle mapper copies IDs from the observed span. |
| Client contract | The normalized envelope defines `session.id`, `type`, `time`, `source`, optional correlation fields, and event-specific `data`. |
| Collector routing | The current local logs pipeline exports to both `otlp/event-exporter` and ClickHouse. The traces pipeline exports to ClickHouse. |
| A2A baseline | The random-number HTTP example exports Observe telemetry and supplies a native message UUID. |
| Third-party OTel example | `third_party_otel.py` owns its TracerProvider and exports GenAI spans plus mapped runtime-event logs to an OTLP/HTTP Collector. |
| Local event demonstration | The realtime demo prints events through an in-process listener, but keeps its spans in an in-memory exporter by default. |

The normalized client schema is a contract, not proof that the external
normalizer populates every field. Actual callback payloads must be captured and
validated at the delivery boundary.

### Current Collector configuration

[The local configuration](../../deploy/otel-collector-config.yaml) currently
sets the event-exporter destination to `http://host.docker.internal:4417` with
`tls.insecure: true`, under an `otlp/event-exporter` exporter in the logs pipeline.
This is intended as plaintext OTLP/gRPC to a service on the Docker host.

Confirm the endpoint syntax is accepted by the deployed Collector version,
the exporter actually serves the OTLP LogsService on that port, and the hostname
resolves from the Collector container. A reachable TCP port is necessary but is
not proof of a successful OTLP logs export. `localhost` inside the Collector
container would address the Collector container, not a host-side exporter.
Other deployment environments may need a service DNS name instead.

The configuration does not explicitly define callback delivery policy or a
durable replay queue. Collector defaults and external exporter behavior must
be recorded rather than assumed.

## How event trace/span IDs map to completed spans

**These are the same OpenTelemetry identifiers, not separately generated event
identifiers.** For a directly span-correlated event:

```text
source span context
    trace_id = T, span_id = S
           |
           +-> OTel LogRecord trace_id = T, span_id = S
           +-> event attributes trace.id = hex(T), span.id = hex(S)
           |
           +-> exported completed span TraceId = T, SpanId = S

normalizer -> callback correlation.trace_id = hex(T)
                       correlation.span_id = hex(S)

CE -> OXP Analytics lookup using BOTH T and S
```

| Representation | Trace ID | Span ID |
|---|---|---|
| OTLP log / span wire format | 16-byte ID | 8-byte ID |
| SDK span context | Integer value | Integer value |
| Runtime-event attributes | `trace.id`: 32 lowercase hex characters | `span.id`: 16 lowercase hex characters |
| Normalized callback | `correlation.trace_id` | `correlation.span_id` |
| Analytics/storage | API-specific field names and encoding; must be mapped to the same values. | API-specific field names and encoding; must be mapped to the same values. |

For example, a callback with:

```json
{
  "correlation": {
    "trace_id": "1352fcaf27e873191676eedad808375c",
    "span_id": "97f7b8fbab698241"
  }
}
```

should select a completed span with exactly that pair. Preserve leading zeros
when converting encodings. Use the actual Analytics API's documented query
fields; this repository does not define its request syntax.

Important distinctions:

- A trace ID selects a trace, not a specific span. Match the pair for an exact
  span lookup, within the caller's authorized service/tenant scope.
- Several events can reference the same span, including started/completed
  events. The pair is not a unique event ID.
- An event can reference an enclosing or propagated span rather than the
  operation span a consumer expected. Record which span was actually found.
  In particular, A2A wire context and send-event context do not always match;
  see the Phase 2 plan.
- `correlation.agent_trace_id` / `agent_span_id`, when mapped, identify an
  enclosing agent invocation. They are not replacements for the event's own
  correlation pair.
- Observe-generated LLM `llm.call.id` uses the model span's hex span ID on the
  current mapper/emission paths. Tool `gen_ai.tool.call.id` is producer-defined
  and must not be assumed to equal a span ID.
- `session.id` groups application activity and can span multiple traces.
  `message_id` pairs protocol messages where preserved. Neither is an exact
  trace/span lookup by itself.
- Missing/zero IDs do not identify a usable span. Flags indicating sampling do
  not guarantee that export, persistence, or Analytics visibility succeeded.

### Realtime does not mean the span is already queryable

Started events may arrive before a span ends. Completed events may also arrive
before span export or Analytics ingestion. The CE should use an agreed bounded
wait/retry policy, with a timeout outcome, rather than declaring the first empty
lookup a permanent mismatch.

Record event arrival, operation completion, first successful span lookup, and
the wait limit. Classify a not-yet-visible span separately from missing IDs,
sampling/export loss, incorrect field mapping, wrong query scope, or referencing
an unexpected span. No fixed ingestion-delay guarantee is established here.

## What is missing or requires agreement

| Gap | Required action |
|---|---|
| Deployed subscription/normalization contract is external. | Record API/version, registration format, supported filter syntax, authentication, and actual field mappings. |
| CE event requirements are not defined. | CASA and Security Detection owners must each agree event types and normalized-field filters. |
| Analytics API contract is external. | Confirm authorized exact-pair lookup, encoding, completed-span visibility, and bounded-wait behavior. |
| CE callback retry/acknowledgement semantics are unknown. | Exercise errors/timeouts and determine which layer acknowledges, queues, retries, drops, or retains delivery. |
| No portable event ID or replay cursor in the client schema. | Document available external delivery identifiers and deduplication/replay mechanisms; do not use span IDs or `session.sequence` as unique delivery IDs. |
| Pure OTel spans do not automatically create runtime-event logs. | Provide a mapper/adapter or native compatible event emitter. OTLP compatibility alone is insufficient. |
| Existing third-party example still imports Observe's mapper. | Agree whether an application-owned OTel provider satisfies the non-Observe criterion; a strictly Observe-free producer requires a separate adapter/emitter demonstration. |
| Local realtime demo uses in-memory spans. | Use the A2A baseline or configure a Collector-backed span exporter; an in-process event print does not satisfy CE delivery or Analytics correlation. |

## Execution plan

### 1. Record prerequisites and CE subscriptions

Record the Collector/exporter versions and configuration, callback URLs,
subscription API contract, OXP Analytics endpoint/contract, authorized identities,
and sample producer versions. Keep tokens and sensitive captures out of source
control.

Complete this matrix with both CE owners before execution:

| Consumer | Proposed baseline | Example filter, subject to actual syntax | Owner-agreed additional needs |
|---|---|---|---|
| CASA | `a2a.message.sent` from the HTTP sample | `type == "a2a.message.sent"` plus agreed service/session scope | Agent output, LLM/tool selections, or other event families needed for context. |
| Security Detection | The same `a2a.message.sent` from that session | The same type filter with its own subscription/callback | Additional security signals or payload fields needed for detection. |

The common A2A event is a proposed delivery/correlation baseline, not proof that
it is sufficient for either CE's business checks. Owners can select another
event available in the agreed sample. For every required type, record the exact
filter, required fields, optional content, and relevant capture configuration.

### 2. Verify Collector-to-exporter delivery

From the Collector container/network, verify DNS resolution and connectivity
to the configured exporter host/port. Send a real OTLP log through the Collector
and capture successful receipt at the event exporter. Record protocol, TLS mode,
endpoint, and diagnostic evidence. Verify traces separately reach the backend
used by OXP Analytics.

Identify the acknowledgement boundary: accepting an OTLP batch is not the same
as successfully delivering that batch's events to both CE callbacks.

### 3. Run the Observe-instrumented sample

Run the [HTTP A2A example](README.md) with the Collector endpoint configured.
Capture its actual session ID, message ID, raw runtime-event log, normalized
callback payloads, and completed span export.

Demonstrate that CASA and Security Detection each receive at least one agreed
event from the same sample session. For a shared event, compare its type,
session, correlation fields, and message ID at both callbacks. Record delivery
timestamps and callback acknowledgements. Do not require identical arrival
order.

### 4. Correlate each captured event through Analytics

For every event in the agreed test set, capture its session ID and emitted
trace/span/call IDs. Query OXP Analytics using the exact trace/span pair and
record the matched completed span, its operation/service, and visibility delay.
Call-ID queries are supplementary unless the API explicitly defines their
uniqueness and scope.

Use an evidence table:

| Event/session | Trace/span pair | Call/message IDs | Analytics match | Delay | Uncorrelated reason |
|---|---|---|---|---|---|
| Populate from captures. | Preserve source values. | Mark absent fields. | Record query and returned span. | Record elapsed time. | Missing IDs, not completed, timeout, sampled out, export failure, mapping/scope error, or unexpected span reference. |

Record events that cannot be correlated; do not invent identifiers to turn a
failure into a match.

### 5. Compare a non-Observe OTel-compatible run (P2)

Use [the third-party OTel example](../realtime_demo/third_party_otel.py) as an
initial application-owned-provider baseline:

```bash
# From the repository root, with the Collector running.
uv run python examples/realtime_demo/third_party_otel.py
```

It uses no `Observe.init()` or Observe decorators, but does use
`RuntimeEventSpanProcessor` from Observe. Its default `gen_ai.conversation.id`
is `conversation-123`; use a distinct run identity for repeated PoC runs to
avoid conflating separate executions.

Capture the same callback and Analytics evidence, adjusting subscriptions for
its agent/model/tool events rather than expecting A2A events.

| Field/capability | Observe A2A baseline | Third-party OTel example |
|---|---|---|
| Session | Observe-generated `session.id`. | `gen_ai.conversation.id` mapped to `session.id`. |
| Trace/span IDs | Supplied/current context at event emission. | IDs of the observed GenAI span. |
| Event families | A2A, agent, session, topology as emitted. | Agent, LLM, and tool lifecycle mappings; no A2A or automatic session/edge events from this mapper. |
| Call ID | Not applicable to A2A; message ID is separate. | Model call ID from span ID; tool call ID from `gen_ai.tool.call.id`. |
| Instrumentation scope | Runtime-event logger scope on the direct emitter path. | Source span instrumentation scope. |
| Input/output | Producer-defined; dependent on event family and content capture. | Captured GenAI messages/arguments/results; JSON decoding and shape preservation require normalizer support. |
| Agent invocation references | Present only when populated and mapped. | Not guaranteed by merely supplying `gen_ai.agent.name`; requires explicit linking attributes. |
| Snapshot version | Observe session graph/event version. | Processor-local per-session sequence; not a global replay cursor. |

If P2 requires zero Observe dependency, add a separate OTel producer that emits
compatible runtime-event logs, or demonstrate a downstream mapper. Document
every missing field and transformation. Do not claim the existing example is
fully Observe-free.

### 6. Exercise callback failures

Test at least a callback returning HTTP 500 and one that times out or never
responds. Include a permanent HTTP 4xx case if the delivery contract distinguishes
retryable and non-retryable errors. Keep the other CE callback healthy.

For each case, capture:

- What the Collector observes: successful OTLP acknowledgement, export error,
  retries, backpressure, or queue exhaustion.
- What the event exporter observes: callback status/timeout, attempts, retry
  schedule, retry limit, retention, and any dead-letter/replay facility.
- Whether the healthy CE continues receiving events independently.
- Whether the failed event is received after recovery, duplicated, dropped,
  retained for manual replay, or of unknown disposition.

Set an agreed observation window and retry budget. Exercise recovery and, where
durability is claimed, restart the component that owns pending delivery.
Distinguish "not delivered yet" from "lost"; lack of evidence must be labelled
unknown, not recoverable.

Collector export retries and CE callback retries are different layers. A
successful Collector export can coexist with a failed CE delivery. Logs stored
in ClickHouse may provide forensic evidence, but do not establish a working
callback replay mechanism.

## Acceptance criteria and deliverables

| Acceptance criterion | Pass evidence |
|---|---|
| Collector sends OTLP logs over gRPC to a reachable exporter. | Container-network connectivity plus actual successful LogsService export/receipt. |
| Both CEs receive agreed events from one sample session. | Saved subscription filters and sanitized POST/acknowledgement captures for CASA and Security Detection. |
| CEs identify session and emitted correlation IDs. | Actual normalized fields mapped to source logs and exact completed-span matches through OXP Analytics; all unmatched events explained. |
| Observe and non-Observe OTel runs are demonstrated (P2). | Producer definitions, adapter/dependency disclosure, field comparison, callbacks, and Analytics evidence for each run. |
| Callback error/nonresponse behavior is documented. | Failure and recovery captures explaining Collector acknowledgement, callback retries, event loss/retention, duplicates, and demonstrated recoverability. |

Deliver an evidence bundle containing deployment configuration/version,
subscription matrix, normalized-field mapping, callback captures, event-to-span
correlation results, producer comparison, callback-failure results, and remaining
gaps. Do not claim end-to-end success from local SDK listener output alone.

## Source references

- [Collector configuration](../../deploy/otel-collector-config.yaml)
- [Collector deployment](../../deploy/docker-compose.yaml)
- [Runtime-event emitter](../../ioa_observe/sdk/tracing/runtime_event_emitter.py)
- [GenAI span-to-event mapper](../../ioa_observe/sdk/tracing/runtime_event_processor.py)
- [Client event schema documentation](../../docs/real-time-event-schema.md)
- [Client JSON Schema](../../schema/realtime_client_event.schema.json)
- [Third-party OTel example documentation](../realtime_demo/README.md)
