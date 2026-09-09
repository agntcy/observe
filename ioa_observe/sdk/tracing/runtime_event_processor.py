# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

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

from ioa_observe.sdk.tracing.runtime_events import (
    RuntimeEvent,
    RuntimeEventAttribute,
    RuntimeEventName,
)

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
                attributes=event.to_otel_attributes(),
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
        self._lock = RLock()
        self._versions: dict[str, int] = {}
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
            session_id = observation.attributes.get("gen_ai.conversation.id")
            if not isinstance(session_id, str) or not session_id:
                return

            with self._lock:
                if self._shutdown:
                    return
                version = self._versions.get(session_id, 0) + 1
                events = self._mapper(observation, version)
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


class GenAIRuntimeEventMapper:
    """Map current OTel GenAI agent, tool, and inference spans to runtime events."""

    _MODEL_OPERATIONS = frozenset({"chat", "generate_content", "text_completion"})

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
            return self._map_tool(observation, session_id, snapshot_version)
        if operation == "invoke_agent":
            return self._map_agent(observation, session_id, snapshot_version)
        if operation in self._MODEL_OPERATIONS:
            return self._map_model(
                observation,
                session_id,
                snapshot_version,
                operation,
            )
        return ()

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
