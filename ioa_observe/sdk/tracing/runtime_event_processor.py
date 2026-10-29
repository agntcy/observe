# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from types import MappingProxyType
from typing import Any, Protocol

from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.trace import Span, SpanKind

from ioa_observe.sdk.tracing.capability_capture import build_capability_attributes
from ioa_observe.sdk.tracing.runtime_events import (
    GEN_AI_MODEL_OPERATIONS,
    RuntimeEvent,
    RuntimeEventAttribute,
    RuntimeEventName,
    identify_runtime_event,
    span_correlation_attributes,
)
from ioa_observe.sdk.tracing.span_normalizers import (
    DEFAULT_NORMALIZERS,
    SpanAttributeNormalizer,
    normalize_span_attributes,
)
from ioa_observe.sdk.tracing.tool_results import tool_error_message

_logger = logging.getLogger(__name__)


class SpanLifecycle(str, Enum):
    START = "start"
    END = "end"


@dataclass(frozen=True)
class InstrumentationScope:
    name: str
    version: str | None = None
    schema_url: str | None = None


@dataclass(frozen=True)
class SpanLifecycleObservation:
    lifecycle: SpanLifecycle
    name: str
    kind: SpanKind
    attributes: Mapping[str, Any]
    trace_id: int
    span_id: int
    instrumentation_scope: InstrumentationScope
    resource_attributes: Mapping[str, Any]
    observed_time: datetime
    trace_flags: int = 0
    parent_span_id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "attributes",
            MappingProxyType(dict(self.attributes)),
        )
        object.__setattr__(
            self,
            "resource_attributes",
            MappingProxyType(dict(self.resource_attributes)),
        )

    @classmethod
    def from_span(
        cls,
        lifecycle: SpanLifecycle,
        span: Any,
    ) -> SpanLifecycleObservation:
        span_context = span.get_span_context()
        scope = span.instrumentation_scope
        timestamp_ns = (
            span.start_time if lifecycle is SpanLifecycle.START else span.end_time
        )
        if timestamp_ns is None:
            raise ValueError(f"Span has no {lifecycle.value} timestamp")

        return cls(
            lifecycle=lifecycle,
            name=span.name,
            kind=span.kind,
            attributes=MappingProxyType(dict(span.attributes or {})),
            trace_id=span_context.trace_id,
            span_id=span_context.span_id,
            trace_flags=int(span_context.trace_flags),
            parent_span_id=(
                span.parent.span_id if getattr(span, "parent", None) else None
            ),
            instrumentation_scope=InstrumentationScope(
                name=scope.name,
                version=scope.version,
                schema_url=scope.schema_url,
            ),
            resource_attributes=MappingProxyType(dict(span.resource.attributes)),
            observed_time=datetime.fromtimestamp(
                timestamp_ns / 1_000_000_000,
                timezone.utc,
            ),
        )


class RuntimeEventMapper(Protocol):
    def __call__(
        self,
        observation: SpanLifecycleObservation,
        snapshot_version: int,
    ) -> Sequence[RuntimeEvent]: ...


class RuntimeEventEmitter(Protocol):
    """Emitter whose emit method must enqueue without network I/O."""

    def emit(self, event: RuntimeEvent) -> None: ...

    def force_flush(self, timeout_millis: int = 30_000) -> bool: ...

    def shutdown(self) -> None: ...


class OtelLogRuntimeEventEmitter:
    """Emit runtime events through an isolated, batched OTel Logs provider."""

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        headers: Mapping[str, str] | None = None,
        resource: Resource | None = None,
        exporter: LogRecordExporter | None = None,
        max_queue_size: int = 2_048,
        schedule_delay_millis: float = 100,
        export_timeout_millis: float = 30_000,
    ) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be positive")
        if schedule_delay_millis <= 0:
            raise ValueError("schedule_delay_millis must be positive")

        log_exporter = (
            exporter
            if exporter is not None
            else OTLPLogExporter(
                endpoint=endpoint,
                headers=dict(headers) if headers else None,
            )
        )
        self._provider = LoggerProvider(
            resource=resource if resource is not None else Resource.create(),
            shutdown_on_exit=False,
        )
        self._provider.add_log_record_processor(
            BatchLogRecordProcessor(
                log_exporter,
                max_queue_size=max_queue_size,
                max_export_batch_size=min(512, max_queue_size),
                schedule_delay_millis=schedule_delay_millis,
                export_timeout_millis=export_timeout_millis,
            )
        )
        self._loggers: dict[tuple[str, str | None], Any] = {}
        self._lock = RLock()

    def emit(self, event: RuntimeEvent) -> None:
        scope_name = event.instrumentation_scope_name or "ioa_observe.runtime_events"
        scope_key = (scope_name, event.instrumentation_scope_version)
        with self._lock:
            logger = self._loggers.get(scope_key)
            if logger is None:
                logger = self._provider.get_logger(
                    scope_name,
                    event.instrumentation_scope_version,
                )
                self._loggers[scope_key] = logger

        event_name = (
            event.name.value if isinstance(event.name, RuntimeEventName) else event.name
        )
        logger.emit(
            LogRecord(
                timestamp=int(event.event_time.timestamp() * 1_000_000_000),
                observed_timestamp=time.time_ns(),
                trace_id=event.trace_id,
                span_id=event.span_id,
                trace_flags=event.trace_flags,
                severity_text="INFO",
                severity_number=SeverityNumber.INFO,
                body=event_name,
                attributes=identify_runtime_event(event.to_otel_attributes()),
                event_name=event_name,
            )
        )

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._provider.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self._provider.shutdown()


class RuntimeEventSpanProcessor(SpanProcessor):
    """Map span callbacks and enqueue events on a private OTel Logs pipeline."""

    def __init__(
        self,
        mapper: RuntimeEventMapper | None = None,
        emitter: RuntimeEventEmitter | None = None,
        *,
        endpoint: str | None = None,
        headers: Mapping[str, str] | None = None,
        resource: Resource | None = None,
        exporter: LogRecordExporter | None = None,
        max_queue_size: int = 2_048,
        schedule_delay_millis: float = 100,
        export_timeout_millis: float = 30_000,
        normalizers: Sequence[SpanAttributeNormalizer] = DEFAULT_NORMALIZERS,
        derive_events: bool = True,
    ) -> None:
        if emitter is not None and any(
            (
                endpoint is not None,
                headers is not None,
                resource is not None,
                exporter is not None,
                max_queue_size != 2_048,
                schedule_delay_millis != 100,
                export_timeout_millis != 30_000,
            )
        ):
            raise ValueError(
                "Transport options cannot be combined with a custom emitter"
            )

        self._mapper = mapper if mapper is not None else GenAIRuntimeEventMapper()
        self._emitter = (
            emitter
            if emitter is not None
            else OtelLogRuntimeEventEmitter(
                endpoint=endpoint,
                headers=headers,
                resource=resource,
                exporter=exporter,
                max_queue_size=max_queue_size,
                schedule_delay_millis=schedule_delay_millis,
                export_timeout_millis=export_timeout_millis,
            )
        )
        self._normalizers = tuple(normalizers)
        self._lock = RLock()
        self._versions: dict[str, int] = {}
        self._deriver = _DerivedEventTracker() if derive_events else None
        self._shutdown = False

    def on_start(
        self,
        span: Span,
        parent_context: Context | None = None,
    ) -> None:
        self._map_and_enqueue(SpanLifecycle.START, span)

    def on_end(self, span: ReadableSpan) -> None:
        self._map_and_enqueue(SpanLifecycle.END, span)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        with self._lock:
            if self._shutdown:
                return True
        return self._emitter.force_flush(timeout_millis)

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        self._emitter.shutdown()

    def _map_and_enqueue(
        self,
        lifecycle: SpanLifecycle,
        span: Span | ReadableSpan,
    ) -> None:
        try:
            observation = SpanLifecycleObservation.from_span(lifecycle, span)
            if self._normalizers:
                observation = replace(
                    observation,
                    attributes=normalize_span_attributes(
                        observation.attributes,
                        observation.name,
                        self._normalizers,
                    ),
                )
            session_id = observation.attributes.get("gen_ai.conversation.id")
            if not isinstance(session_id, str) or not session_id:
                return

            with self._lock:
                if self._shutdown:
                    return
                version = self._versions.get(session_id, 0) + 1
                events = tuple(self._mapper(observation, version))
                if self._deriver is not None:
                    before, after = self._deriver.derive(
                        observation, session_id, events
                    )
                    events = (*before, *events, *after)
                if not events:
                    return
                sequenced_events = tuple(
                    replace(event, snapshot_version=version + offset)
                    for offset, event in enumerate(events)
                )
                for event in sequenced_events:
                    self._emitter.emit(event)
                    self._versions[session_id] = event.snapshot_version
        except Exception:
            _logger.exception("Failed to map span lifecycle to a runtime event")


def _parse_tool_definitions(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    if isinstance(value, (list, tuple)):
        return list(value)
    return None


@dataclass
class _OpenSpan:
    session_id: str
    owner_agent: str | None
    tools_presented: bool = False


class _DerivedEventTracker:
    """Derive session, edge and tool-presentation events from span structure.

    Third-party SDKs never call Observe's topology helpers, so these events are
    inferred: a session starts when its first span is seen and completes when
    its last open span ends; an edge is a child agent span under an agent span.
    """

    EDGE_CONFIDENCE = 0.8

    def __init__(self) -> None:
        self._open: dict[int, _OpenSpan] = {}
        self._open_count: dict[str, int] = {}
        self._edges: dict[str, set[tuple[str, str]]] = {}

    def derive(
        self,
        observation: SpanLifecycleObservation,
        session_id: str,
        mapped: Sequence[RuntimeEvent],
    ) -> tuple[tuple[RuntimeEvent, ...], tuple[RuntimeEvent, ...]]:
        before: list[RuntimeEvent] = []
        after: list[RuntimeEvent] = []
        attrs = observation.attributes
        operation = attrs.get("gen_ai.operation.name")
        is_start = observation.lifecycle is SpanLifecycle.START

        if is_start:
            if session_id not in self._open_count:
                before.append(
                    self._event(
                        RuntimeEventName.TOPOLOGY_SESSION_STARTED,
                        observation,
                        session_id,
                    )
                )
            parent = self._open.get(observation.parent_span_id)
            parent_agent = parent.owner_agent if parent else None
            agent = self._agent_name(operation, attrs) if mapped else None
            self._open[observation.span_id] = _OpenSpan(
                session_id, agent or parent_agent
            )
            self._open_count[session_id] = self._open_count.get(session_id, 0) + 1
            if agent and parent_agent and agent != parent_agent:
                seen = self._edges.setdefault(session_id, set())
                if (parent_agent, agent) not in seen:
                    seen.add((parent_agent, agent))
                    after.append(
                        self._edge_event(observation, session_id, parent_agent, agent)
                    )

        state = self._open.get(observation.span_id)
        if (
            mapped
            and operation in GEN_AI_MODEL_OPERATIONS
            and (state is None or not state.tools_presented)
        ):
            tools = _parse_tool_definitions(attrs.get("gen_ai.tool.definitions"))
            if tools:
                if state is not None:
                    state.tools_presented = True
                after.append(
                    self._tools_event(observation, session_id, operation, tools)
                )

        if not is_start and state is not None:
            del self._open[observation.span_id]
            remaining = self._open_count.get(session_id, 1) - 1
            if remaining > 0:
                self._open_count[session_id] = remaining
            else:
                self._open_count.pop(session_id, None)
                self._edges.pop(session_id, None)
                after.append(
                    self._event(
                        RuntimeEventName.TOPOLOGY_SESSION_COMPLETED,
                        observation,
                        session_id,
                    )
                )
        return tuple(before), tuple(after)

    @staticmethod
    def _agent_name(operation: Any, attrs: Mapping[str, Any]) -> str | None:
        name = attrs.get("gen_ai.agent.name")
        if operation == "invoke_agent" and isinstance(name, str) and name:
            return name
        return None

    @staticmethod
    def _event(
        name: RuntimeEventName,
        observation: SpanLifecycleObservation,
        session_id: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> RuntimeEvent:
        event = RuntimeEvent(
            name=name,
            session_id=session_id,
            snapshot_version=1,
            event_time=observation.observed_time,
            attributes=dict(attributes or {}),
            trace_id=observation.trace_id,
            span_id=observation.span_id,
            trace_flags=observation.trace_flags,
            instrumentation_scope_name=observation.instrumentation_scope.name,
            instrumentation_scope_version=observation.instrumentation_scope.version,
        )
        event.to_otel_attributes()
        return event

    def _edge_event(
        self,
        observation: SpanLifecycleObservation,
        session_id: str,
        source: str,
        target: str,
    ) -> RuntimeEvent:
        return self._event(
            RuntimeEventName.TOPOLOGY_EDGE_UPDATED,
            observation,
            session_id,
            {
                RuntimeEventAttribute.SOURCE_AGENT.value: source,
                RuntimeEventAttribute.TARGET_AGENT.value: target,
                "topology.edge.id": f"agent_handoff:{source}->{target}",
                "topology.edge.kind": "agent_handoff",
                "topology.edge.status": "started",
                "topology.edge.evidence": "parent_span",
                "topology.edge.confidence": self.EDGE_CONFIDENCE,
                "operation.name": "invoke_agent",
            },
        )

    def _tools_event(
        self,
        observation: SpanLifecycleObservation,
        session_id: str,
        operation: str,
        tools: Any,
    ) -> RuntimeEvent:
        attributes = build_capability_attributes(
            {"tools": tools},
            source="llm-presented",
            attributes={
                "operation.name": operation,
                "llm.name": observation.attributes.get("gen_ai.request.model"),
                "llm.call.id": f"{observation.span_id:016x}",
            },
        )
        return self._event(
            RuntimeEventName.LLM_TOOLS_PRESENTED,
            observation,
            session_id,
            attributes,
        )


class GenAIRuntimeEventMapper:
    """Map current OTel GenAI agent, tool, and inference spans to runtime events."""

    def __call__(
        self,
        observation: SpanLifecycleObservation,
        snapshot_version: int,
    ) -> tuple[RuntimeEvent, ...]:
        if snapshot_version < 1:
            raise ValueError("snapshot_version must be positive")

        session_id = observation.attributes.get("gen_ai.conversation.id")
        if not isinstance(session_id, str) or not session_id:
            return ()

        operation = observation.attributes.get("gen_ai.operation.name")
        if operation == "execute_tool":
            events = self._map_tool(observation, session_id, snapshot_version)
        elif operation == "invoke_agent":
            events = self._map_agent(observation, session_id, snapshot_version)
        elif operation in GEN_AI_MODEL_OPERATIONS:
            events = self._map_model(
                observation,
                session_id,
                snapshot_version,
                operation,
            )
        else:
            return ()
        correlation = span_correlation_attributes(observation.attributes)
        agent_name = observation.attributes.get("gen_ai.agent.name")
        if isinstance(agent_name, str) and agent_name:
            correlation[RuntimeEventAttribute.AGENT_NAME.value] = agent_name
        return tuple(
            replace(event, attributes={**event.attributes, **correlation})
            for event in events
        )

    @staticmethod
    def _map_agent(
        observation: SpanLifecycleObservation,
        session_id: str,
        snapshot_version: int,
    ) -> tuple[RuntimeEvent, ...]:
        if observation.kind not in {SpanKind.INTERNAL, SpanKind.CLIENT}:
            return ()
        agent_name = observation.attributes.get("gen_ai.agent.name")
        if not isinstance(agent_name, str) or not agent_name:
            return ()
        provider_name = observation.attributes.get("gen_ai.provider.name")
        if observation.kind is SpanKind.CLIENT and (
            not isinstance(provider_name, str) or not provider_name
        ):
            return ()

        event_name = (
            RuntimeEventName.TOPOLOGY_NODE_STARTED
            if observation.lifecycle is SpanLifecycle.START
            else RuntimeEventName.TOPOLOGY_NODE_COMPLETED
        )
        attributes = {
            RuntimeEventAttribute.AGENT_NAME.value: agent_name,
            "gen_ai.operation.name": "invoke_agent",
        }
        agent_input = observation.attributes.get("gen_ai.input.messages")
        if agent_input is not None:
            attributes[RuntimeEventAttribute.AGENT_INPUT.value] = agent_input
        if observation.lifecycle is SpanLifecycle.END:
            agent_output = observation.attributes.get("gen_ai.output.messages")
            if agent_output is not None:
                attributes[RuntimeEventAttribute.AGENT_OUTPUT.value] = agent_output
        if isinstance(provider_name, str) and provider_name:
            attributes["gen_ai.provider.name"] = provider_name
        event = RuntimeEvent(
            name=event_name,
            session_id=session_id,
            snapshot_version=snapshot_version,
            event_time=observation.observed_time,
            attributes=attributes,
            trace_id=observation.trace_id,
            span_id=observation.span_id,
            trace_flags=observation.trace_flags,
            instrumentation_scope_name=observation.instrumentation_scope.name,
            instrumentation_scope_version=observation.instrumentation_scope.version,
        )
        event.to_otel_attributes()
        return (event,)

    @staticmethod
    def _map_tool(
        observation: SpanLifecycleObservation,
        session_id: str,
        snapshot_version: int,
    ) -> tuple[RuntimeEvent, ...]:
        if observation.kind is not SpanKind.INTERNAL:
            return ()
        tool_name = observation.attributes.get("gen_ai.tool.name")
        if not isinstance(tool_name, str) or not tool_name:
            return ()

        attributes: dict[str, Any] = {
            RuntimeEventAttribute.TOOL_NAME.value: tool_name,
            "gen_ai.operation.name": "execute_tool",
        }
        optional_mappings = {
            "gen_ai.tool.call.id": "gen_ai.tool.call.id",
            "gen_ai.tool.call.arguments": RuntimeEventAttribute.TOOL_INPUT.value,
            "gen_ai.tool.call.result": RuntimeEventAttribute.TOOL_OUTPUT.value,
            "gen_ai.agent.name": "gen_ai.agent.name",
        }
        for source, target in optional_mappings.items():
            value = observation.attributes.get(source)
            if value is not None:
                attributes[target] = value

        event_name = (
            RuntimeEventName.TOOL_STARTED
            if observation.lifecycle is SpanLifecycle.START
            else RuntimeEventName.TOOL_COMPLETED
        )
        if event_name is RuntimeEventName.TOOL_COMPLETED:
            error_message = tool_error_message(
                attributes.get(RuntimeEventAttribute.TOOL_OUTPUT.value)
            )
            attributes[RuntimeEventAttribute.TOOL_STATUS.value] = (
                "error" if error_message else "success"
            )
            if error_message:
                attributes[RuntimeEventAttribute.TOOL_ERROR_MESSAGE.value] = (
                    error_message
                )
        event = RuntimeEvent(
            name=event_name,
            session_id=session_id,
            snapshot_version=snapshot_version,
            event_time=observation.observed_time,
            attributes=attributes,
            trace_id=observation.trace_id,
            span_id=observation.span_id,
            trace_flags=observation.trace_flags,
            instrumentation_scope_name=observation.instrumentation_scope.name,
            instrumentation_scope_version=observation.instrumentation_scope.version,
        )
        event.to_otel_attributes()
        return (event,)

    @staticmethod
    def _map_model(
        observation: SpanLifecycleObservation,
        session_id: str,
        snapshot_version: int,
        operation: str,
    ) -> tuple[RuntimeEvent, ...]:
        if observation.kind not in {SpanKind.CLIENT, SpanKind.INTERNAL}:
            return ()
        provider_name = observation.attributes.get("gen_ai.provider.name")
        if not isinstance(provider_name, str) or not provider_name:
            return ()

        request_model = observation.attributes.get("gen_ai.request.model")
        response_model = observation.attributes.get("gen_ai.response.model")
        model_name = (
            request_model
            if observation.lifecycle is SpanLifecycle.START
            else response_model or request_model
        )
        if not isinstance(model_name, str) or not model_name:
            return ()

        attributes: dict[str, Any] = {
            RuntimeEventAttribute.LLM_NAME.value: model_name,
            RuntimeEventAttribute.LLM_CALL_ID.value: f"{observation.span_id:016x}",
            "gen_ai.operation.name": operation,
            "gen_ai.provider.name": provider_name,
        }
        optional_mappings = {
            "gen_ai.input.messages": RuntimeEventAttribute.LLM_INPUT.value,
            "gen_ai.output.messages": RuntimeEventAttribute.LLM_OUTPUT.value,
            "gen_ai.response.id": "gen_ai.response.id",
        }
        for source, target in optional_mappings.items():
            value = observation.attributes.get(source)
            if value is not None:
                attributes[target] = value

        event_name = (
            RuntimeEventName.LLM_STARTED
            if observation.lifecycle is SpanLifecycle.START
            else RuntimeEventName.LLM_COMPLETED
        )
        event = RuntimeEvent(
            name=event_name,
            session_id=session_id,
            snapshot_version=snapshot_version,
            event_time=observation.observed_time,
            attributes=attributes,
            trace_id=observation.trace_id,
            span_id=observation.span_id,
            trace_flags=observation.trace_flags,
            instrumentation_scope_name=observation.instrumentation_scope.name,
            instrumentation_scope_version=observation.instrumentation_scope.version,
        )
        event.to_otel_attributes()
        return (event,)
