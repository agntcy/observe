# Phase 2 PoC: A2A Enforcement and Cognition Store

## Objective

Demonstrate that an enforcement proxy attached to the sample agents can inspect
an A2A message, extract its propagated observability context without a separate
lookup, and pass the required identifiers to Cognition Fabric (CF) for a
Cognition Engine (CE) evaluation. Capture the resulting disposition and
demonstrate an authorized Cognition Store write followed by a read of the same
record.

This is a plan, not a claim that enforcement or store integration is implemented.
The external enforcement contract referenced in the acceptance criteria was not
provided with this writeup. Its URL and version must be recorded before execution;
exact field names, required identifiers, authentication, and failure semantics
must be validated against that contract.

## Proposed flow

```text
Source agent
  -> Observe injects context into the outgoing A2A message
  -> Proxy intercepts the actual A2A request
  -> Proxy validates and extracts identifiers
  -> Proxy calls CF Enforcement/GetDisposition
  -> CF calls CE Evaluate with the protected action and context
  -> CE returns an evaluation to CF
  -> CF returns a disposition to the proxy
  -> Proxy allows, denies, or modifies forwarding
```

The proposed API names are
`ioc.cf.enforcement.v1.Enforcement/GetDisposition` and `ioc.ce.v1.Evaluate`.
They come from the proposed integration flow, not an implementation or API
definition in this repository. CE is the decision engine called by CF; the proxy
is the enforcement client. Capture the CF-to-CE request and CE-to-CF response
where the deployment permits it, as well as the proxy-facing request/response.

## What is available today

| Capability | Current behavior |
|---|---|
| A2A sample application | HTTP random-number client/server; hello-world HTTP examples; optional SLIM-A2A client/server. |
| Example dependencies | `a2a-sdk==1.1.0`; `slima2a==0.7.0` for SLIM. |
| Context injection | Observe adds W3C `traceparent` and available session, agent-linking, and fork metadata to A2A metadata. |
| Context extraction | Destination instrumentation reads Observe metadata and attaches the propagated context before invoking the handler. |
| Runtime events | `a2a.message.sent`, `a2a.message.received`, and associated topology edge updates. |
| Event correlation | OTel log correlation and `trace.id` / `span.id` attributes reference the context active when the event is emitted. |
| Message correlation | A native A2A message ID is emitted as `message.id` when present. The random-number client explicitly supplies a UUID. |
| Diagnostics | Example clients print metadata before transport invocation; servers print received metadata before agent execution. Output includes raw `traceparent`, decoded IDs, validity, and metadata location. |

### Metadata location and meaning

`metadata.observe.traceparent` describes a metadata-relative path, not an HTTP
header. With the pinned sample SDK and current Observe injection, it is stored
under:

```text
request.message.metadata.observe.traceparent
```

The serialized JSON body uses the corresponding `message.metadata.observe`
object. Other SDK/request shapes may place it in outer request/params metadata.
The proxy must inspect the actual transport payload and document the paths it
supports; the application interceptor print is not a wire capture.

Example:

```json
{
  "message": {
    "messageId": "msg-123",
    "metadata": {
      "observe": {
        "traceparent": "00-1352fcaf27e873191676eedad808375c-97f7b8fbab698241-01",
        "session_id": "demo-session"
      }
    }
  }
}
```

For this valid W3C value, extraction gives:

| Component | Value | Meaning |
|---|---|---|
| Version | `00` | W3C trace-context version. |
| Trace ID | `1352fcaf27e873191676eedad808375c` | Propagated trace identity. |
| Parent/span ID | `97f7b8fbab698241` | Propagated source span identity, not a newly created proxy or destination span. |
| Trace flags | `01` | Sampled flag set; not an authorization decision. |

No Observe, analytics, or Cognition Store lookup is needed to decode these
components. Session identity and message identity come from separate fields:
`observe.session_id` and the native A2A message ID. They cannot be derived from
`traceparent`.

Use a W3C-aware parser. Reject invalid IDs rather than accepting arbitrary
hyphen-separated text. A valid unsampled context (`00` flags) remains usable for
correlation even if the corresponding telemetry is not exported.

## Operations in scope

| Path | Instrumented operations | PoC coverage |
|---|---|---|
| BaseClient | `send_message`; separate `send_message_streaming` when exposed by the SDK. | Required HTTP baseline. In SDK 1.1.0, `send_message` selects ordinary or streaming transport behavior. |
| Server handler | `on_message_send`, `on_message_send_stream` when available. | Cover the receive path corresponding to each exercised send mode. |
| Legacy client | `send_message`; `broadcast_message` when available. | Conditional: exercise only if the selected application/version uses it. |
| SLIM-A2A transport | `send_message`, `send_message_streaming`. | Conditional: requires a SLIM gateway and a proxy capable of inspecting that transport. |

The random-number example is the ordinary HTTP baseline. Streaming needs a
streaming-capable sample configuration, such as the hello-world server.
Broadcast requires a compatible client/application; it is not assumed to be
available in the pinned BaseClient.

Observe does not currently provide dedicated A2A hooks here for task get/cancel,
subscriptions, or response-direction enforcement. Application delegation and
tool requests transported over A2A are observed as messages; their action class
must be supplied or determined according to the enforcement contract.
Streaming response chunks are not separate outgoing request-metadata captures.
Response inspection and request/response pairing must be demonstrated separately
if required by the sample application.

## What is missing or not guaranteed

| Gap | Consequence |
|---|---|
| Proxy/CF/CE/store integration is not supplied by these examples. | Metadata printing alone does not demonstrate enforcement, forwarding, or persistence. |
| External enforcement contract is not included here. | Required IDs, request mapping, dispositions, and fail-open/closed rules remain external dependencies. |
| Stored incoming `traceparent` takes precedence in `get_current_traceparent()`. | Nested outgoing actions can reuse the incoming pair instead of identifying their outbound A2A spans. |
| Legacy and SLIM transport send events are emitted after their send-span scope exits. | Wire IDs can differ from the send event's trace/span IDs. BaseClient emits inside its send span but still has the stored-context caveat. |
| Native message ID is not guaranteed by Observe on every path. | A universal message-level join cannot be assumed without an explicit producer/contract requirement. |
| Trace/span IDs identify context, not an event or enforcement attempt. | Several messages/events/evaluations can share a pair. Retries need separately defined attempt identity when relevant. |
| Analytics may arrive later or not at all. | Context extraction must not depend on an analytics lookup. CE context sufficiency and bounded-wait behavior are separate concerns. |
| Metadata is not proof of identity or authorization. | Bind enforcement to authenticated agent identity; validate correlation fields without trusting them as credentials. |

For message-level correlation, propose `(session_id, message_id)` when both are
present and preserved, subject to the enforcement contract's scope and uniqueness
requirements. Retain the source `(trace_id, span_id)` as tracing context as well.
Do not substitute `snapshot.version`, agent sequence, or topology edge ID for
message/attempt identity.

## Execution plan

### 1. Agree prerequisites and contract mapping

Record the enforcement contract URL/version, selected sample operations,
proxy placement, CF/CE endpoints, authorized agent identities, and Cognition
Store credentials and API contract. Do not commit credentials or tokens.

Create a mapping for every required contract field: actual wire source,
validation rule, CF request field, CE request field, and missing-field policy.
Include trace ID, source span ID, session ID, message ID, action class, and
direction where required. Optional agent/fork metadata must not silently become
required unless the contract says so.

Decide whether exact wire-to-send-event span equality is an acceptance
requirement. If it is, the current instrumentation gaps require changes before
that criterion can pass. Do not claim success by matching trace ID alone.

### 2. Run the HTTP baseline through the proxy

Follow [the example setup](README.md), routing agent traffic through the proxy
rather than directly to the destination. Verify agent-card advertised URLs do
not bypass interception. Keep example metadata diagnostics enabled.

For each selected operation, capture:

| Boundary | Required evidence |
|---|---|
| Source to proxy | Actual method/transport, metadata location, raw `traceparent`, native message ID, session ID, and authenticated source identity. |
| Proxy extraction | Parsed source trace/span IDs, flags, validation result, and missing-field handling. |
| Proxy to CF | Sanitized enforcement request showing the exact extracted values and their contract field mapping. |
| CF to CE | Sanitized evaluation request when accessible; distinguish protected source IDs from new CF/CE tracing spans. |
| CE/CF response | Outcome, denial reason where applicable, and identifiers linking it to the inspected action. |
| Proxy forwarding | Evidence of allow, deny, or modification behavior for the returned disposition. |
| Observe stream | Relevant send/receive event fields and matching span evidence when available. Record mismatches explicitly. |

Demonstrate that parsing and constructing the enforcement request requires no
separate lookup. Optional CE analytics queries are not part of identifier
extraction and should be recorded separately.

### 3. Exercise correlation and failure cases

| Case | Expected evidence |
|---|---|
| Valid sampled context | Extracted IDs equal the raw wire value and are passed unchanged in the protected-context fields. |
| Valid unsampled context | IDs remain extractable; absence of exported telemetry is not treated as malformed metadata. |
| Nested A -> B -> C | Capture B's incoming context and outgoing context; record any stored-context reuse. |
| Two distinct messages from B | Compare message IDs, wire pairs, and send-event pairs; expose any shared tracing context. |
| Missing Observe metadata or traceparent | Explicit validation outcome and recorded policy; no invented source IDs. |
| Malformed traceparent or zero trace/span ID | Explicit invalid-context outcome and recorded policy; no fabricated source context or silent fallback. |
| Required session/message ID missing | Apply the documented contract policy; do not infer these IDs from traceparent. |
| CF/CE timeout, unavailability, or invalid disposition | Document the proxy's configured policy separately from malformed-metadata handling. |
| Retry or repeated inspection | Record how the same message is distinguished from a new enforcement attempt if required. |

For each failure case, record whether forwarding is blocked (fail closed) or
allowed (fail open), the reason, and whether CF/CE is called. The current Observe
examples define neither policy. The enforcement owner must approve it before
execution; fail closed is the proposed default for protected actions, not an
existing implementation guarantee.

### 4. Demonstrate Cognition Store persistence

Using an authorized CE/service principal, write one finding through the actual
Cognition Store API and capture its acknowledged record key. Include the
protected-action correlation fields supported by the store contract.

Read that same key using an authorized principal and compare the persisted
finding and correlation values with the acknowledged write. Record any required
visibility delay or bounded retry rule. A local print, mocked response, or
analytics event is not evidence of an authorized store write/read.

Capture sanitized request/response evidence and record keys, not credentials.
Follow the environment's cleanup policy for the PoC record.

## Acceptance criteria and deliverables

| Acceptance criterion | Pass evidence |
|---|---|
| Exercise relevant A2A operations. | Completed operation matrix with wire capture, proxy extraction, CF request, returned disposition, and actual forwarding outcome for each selected operation. |
| Validate required IDs against the enforcement contract. | Contract URL/version and field mapping; extracted IDs match the actual message and are retained through CF/CE. Any event/span alignment requirement is assessed separately. |
| Obtain IDs without a separate lookup. | Proxy extraction is local to the intercepted payload and does not depend on Observe, analytics, or store availability. |
| Define missing/malformed behavior. | Executed failure cases documenting fail-open/closed behavior and matching the approved policy. |
| Demonstrate an authorized Cognition Store write/read. | Acknowledged write and subsequent read of the same record with matching content and correlation fields. |

Deliver an evidence bundle with an operation matrix, sanitized boundary captures,
contract mapping, failure-policy results, store write/read evidence, and a gap
list. Distinguish implemented behavior, observed limitations, and proposed fixes.
Keep sensitive captures outside source control.

## Source references

- [Example setup and metadata diagnostics](README.md)
- [A2A instrumentation](../../ioa_observe/sdk/instrumentations/a2a.py)
- [Traceparent helper](../../ioa_observe/sdk/tracing/tracing.py)
- [Runtime-event emission](../../ioa_observe/sdk/tracing/runtime_event_emitter.py)
- [Client event schema documentation](../../docs/real-time-event-schema.md)
