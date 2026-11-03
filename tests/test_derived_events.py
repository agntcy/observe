# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import json

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind

from ioa_observe.sdk.tracing import RuntimeEventSpanProcessor


class ListEmitter:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    def force_flush(self, timeout_millis=30_000):
        return True

    def shutdown(self):
        pass


def _tracer(**kwargs):
    emitter = ListEmitter()
    provider = TracerProvider()
    provider.add_span_processor(RuntimeEventSpanProcessor(emitter=emitter, **kwargs))
    return provider.get_tracer("vendor.sdk"), emitter


def _agent(name):
    return {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": name,
        "gen_ai.conversation.id": "s1",
    }


def test_session_edge_and_tools_events_from_span_tree():
    tracer, emitter = _tracer()
    tools = [{"type": "function", "name": "lookup"}]
    with tracer.start_as_current_span("a", attributes=_agent("planner")):
        with tracer.start_as_current_span("b", attributes=_agent("worker")):
            with tracer.start_as_current_span(
                "chat",
                kind=SpanKind.CLIENT,
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": "openai",
                    "gen_ai.request.model": "gpt-4o",
                    "gen_ai.conversation.id": "s1",
                    "gen_ai.tool.definitions": json.dumps(tools),
                },
            ):
                pass

    names = [event.name for event in emitter.events]
    assert names[0] == "topology.session.started"
    assert names[-1] == "topology.session.completed"
    assert names.count("topology.session.started") == 1
    assert names.count("topology.edge.updated") == 1
    assert names.count("llm.tools.presented") == 1

    edge = next(e for e in emitter.events if e.name == "topology.edge.updated")
    assert edge.attributes["source.agent"] == "planner"
    assert edge.attributes["target.agent"] == "worker"
    assert edge.attributes["topology.edge.evidence"] == "parent_span"

    presented = next(e for e in emitter.events if e.name == "llm.tools.presented")
    assert presented.attributes["capability.source"] == "llm-presented"
    assert presented.attributes["llm.name"] == "gpt-4o"
    assert presented.attributes["capability.capture.state"] in {
        "complete",
        "disabled",
    }

    versions = [event.snapshot_version for event in emitter.events]
    assert versions == sorted(versions) and len(set(versions)) == len(versions)


def test_same_agent_nesting_makes_no_edge():
    tracer, emitter = _tracer()
    with tracer.start_as_current_span("a", attributes=_agent("planner")):
        with tracer.start_as_current_span("b", attributes=_agent("planner")):
            pass
    assert "topology.edge.updated" not in [e.name for e in emitter.events]


def test_new_session_emitted_after_completion():
    tracer, emitter = _tracer()
    for _ in range(2):
        with tracer.start_as_current_span("a", attributes=_agent("planner")):
            pass
    names = [e.name for e in emitter.events]
    assert names.count("topology.session.started") == 2
    assert names.count("topology.session.completed") == 2


def test_derive_events_can_be_disabled():
    tracer, emitter = _tracer(derive_events=False)
    with tracer.start_as_current_span("a", attributes=_agent("planner")):
        with tracer.start_as_current_span("b", attributes=_agent("worker")):
            pass
    assert {e.name for e in emitter.events} == {
        "topology.node.started",
        "topology.node.completed",
    }


def test_edge_derived_when_agent_names_are_set_after_start():
    # Traceloop sets entity attributes after the span has started.
    tracer, emitter = _tracer()
    with tracer.start_as_current_span(
        "wf", attributes={"gen_ai.conversation.id": "s1"}
    ):
        with tracer.start_as_current_span(
            "p", attributes={"gen_ai.conversation.id": "s1"}
        ) as p:
            with tracer.start_as_current_span(
                "w", attributes={"gen_ai.conversation.id": "s1"}
            ) as w:
                w.set_attributes(
                    {
                        "gen_ai.operation.name": "invoke_agent",
                        "gen_ai.agent.name": "worker",
                    }
                )
            p.set_attributes(
                {
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": "planner",
                }
            )
    edges = [e for e in emitter.events if e.name == "topology.edge.updated"]
    assert [
        (e.attributes["source.agent"], e.attributes["target.agent"]) for e in edges
    ] == [("planner", "worker")]


def _late_agent_run(**kwargs):
    tracer, emitter = _tracer(derive_events=False, **kwargs)
    with tracer.start_as_current_span(
        "a", attributes={"gen_ai.conversation.id": "s1"}
    ) as span:
        span.set_attributes(
            {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "late"}
        )
    return emitter


def test_start_event_backfilled_when_attributes_set_after_start():
    emitter = _late_agent_run()
    assert [e.name for e in emitter.events] == [
        "topology.node.started",
        "topology.node.completed",
    ]
    started, completed = emitter.events
    assert started.event_time <= completed.event_time
    assert started.snapshot_version < completed.snapshot_version


def test_backfill_can_be_disabled():
    emitter = _late_agent_run(backfill_start_events=False)
    assert [e.name for e in emitter.events] == ["topology.node.completed"]


def test_no_duplicate_start_when_attributes_present_at_start():
    tracer, emitter = _tracer(derive_events=False)
    with tracer.start_as_current_span("a", attributes=_agent("planner")):
        pass
    assert [e.name for e in emitter.events] == [
        "topology.node.started",
        "topology.node.completed",
    ]
