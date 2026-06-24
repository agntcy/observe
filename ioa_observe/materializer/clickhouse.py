# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import requests

from ioa_observe.materializer.session_state import (
    RuntimeEventRecord,
    SessionStateMaterializer,
)
from ioa_observe.sdk.tracing.runtime_events import (
    RuntimeEventAttribute,
    RuntimeEventName,
)


_RUNTIME_EVENT_NAMES = tuple(event_name.value for event_name in RuntimeEventName)


class ClickHouseRuntimeEventSource:
    def __init__(
        self,
        base_url: str,
        *,
        username: str,
        password: str,
        table: str = "otel_logs",
        timeout_seconds: float = 10.0,
        session: requests.Session | Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.table = table
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def fetch_events(
        self,
        *,
        since_timestamp: str | None = None,
        service_name: str | None = None,
        limit: int = 500,
    ) -> list[RuntimeEventRecord]:
        query = self._build_query(
            since_timestamp=since_timestamp,
            service_name=service_name,
            limit=limit,
        )
        response = self.session.get(
            self.base_url,
            params={"query": query},
            auth=(self.username, self.password),
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()

        events: list[RuntimeEventRecord] = []
        for line in response.text.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            events.append(self._row_to_event(row))
        return events

    def _build_query(
        self,
        *,
        since_timestamp: str | None,
        service_name: str | None,
        limit: int,
    ) -> str:
        predicates = [
            "EventName IN ("
            + ", ".join(f"'{self._escape(value)}'" for value in _RUNTIME_EVENT_NAMES)
            + ")"
        ]
        if since_timestamp:
            predicates.append(
                f"Timestamp >= toDateTime64('{self._escape(since_timestamp)}', 9)"
            )
        if service_name:
            predicates.append(f"ServiceName = '{self._escape(service_name)}'")

        where_clause = " AND ".join(predicates)
        return (
            f"SELECT Timestamp, EventName, ServiceName, LogAttributes FROM {self.table} "
            f"WHERE {where_clause} ORDER BY Timestamp ASC LIMIT {int(limit)} FORMAT JSONEachRow"
        )

    @staticmethod
    def _row_to_event(row: Mapping[str, Any]) -> RuntimeEventRecord:
        attributes = dict(row.get("LogAttributes") or {})
        attributes.setdefault(
            RuntimeEventAttribute.EVENT_NAME.value,
            row.get("EventName"),
        )
        return RuntimeEventRecord.from_attributes(
            attributes,
            observed_timestamp=row.get("Timestamp"),
            service_name=row.get("ServiceName"),
        )

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")


class ClickHouseRuntimeEventConsumer:
    def __init__(
        self,
        source: ClickHouseRuntimeEventSource,
        materializer: SessionStateMaterializer,
        *,
        service_name: str | None = None,
    ) -> None:
        self.source = source
        self.materializer = materializer
        self.service_name = service_name
        self.cursor: str | None = None

    def poll_once(self, *, limit: int = 500) -> list[RuntimeEventRecord]:
        events = self.source.fetch_events(
            since_timestamp=self.cursor,
            service_name=self.service_name,
            limit=limit,
        )
        if not events:
            return []

        self.materializer.apply_events(events)
        observed_timestamps = [
            event.observed_timestamp for event in events if event.observed_timestamp
        ]
        if observed_timestamps:
            self.cursor = observed_timestamps[-1]
        return events
