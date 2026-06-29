# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

"""Phase 4 — Real-Time Observability demo.

This is a self-contained, one-command demo of the real-time observability
add-on. It runs a small multi-agent workflow and renders the live
``SessionState`` as runtime events are pushed — *before* the corresponding
spans flush.

It deliberately uses the in-process runtime-event listener so the demo needs
no external collector or ClickHouse: the same runtime events that are exported
through OTel are fed straight into the ``SessionStateMaterializer`` and printed
as they arrive.

Run it with::

    ./run.sh
        # or
    uv run python agents.py
"""

from __future__ import annotations

import json
import time

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import agent, tool
from ioa_observe.sdk.tracing import (
    register_runtime_event_listener,
    session_start,
)
from ioa_observe.materializer import SessionStateMaterializer


# A backend-side materializer that turns pushed runtime events into live state.
materializer = SessionStateMaterializer()


def _on_runtime_event(event: dict) -> None:
    """Feed each pushed runtime event into the materializer and show the state.

    In production the materializer lives downstream (it consumes the OTel
    events from the telemetry pipeline). Here we attach it in-process so the
    demo runs with a single command.
    """
    session = materializer.apply_event(event)
    name = event.get("event.name", "?")
    snapshot = session.snapshot()
    active_nodes = [n["id"] for n in snapshot["nodes"] if n["status"] == "started"]
    done_nodes = [n["id"] for n in snapshot["nodes"] if n["status"] == "completed"]
    edges = [f"{e['source']}->{e['target']}" for e in snapshot["edges"]]
    running_tools = [t["name"] for t in snapshot["tools"] if t["status"] == "running"]
    print(
        f"  [event] {name:<26} "
        f"active={active_nodes} done={done_nodes} "
        f"edges={edges} tools_running={running_tools}"
    )


@tool(name="web_search", description="Look up information for the workflow")
def web_search(payload: dict) -> dict:
    time.sleep(0.2)
    return {"results": [f"fact about {payload['topic']}"]}


@agent(name="planner", description="Breaks the task into steps")
def planner(payload: dict) -> dict:
    return {"topic": payload["task"], "plan": ["research", "write"]}


@agent(name="researcher", description="Researches the topic")
def researcher(payload: dict) -> dict:
    findings = web_search({"topic": payload["topic"]})
    return {"topic": payload["topic"], "findings": findings["results"]}


@agent(name="writer", description="Drafts the output")
def writer(payload: dict) -> dict:
    return {"topic": payload["topic"], "draft": f"draft about {payload['topic']}"}


@agent(name="synthesizer", description="Combines the work into a result")
def synthesizer(payload: dict) -> dict:
    return {"result": f"final brief about {payload['topic']}"}


def main() -> None:
    Observe.init(
        app_name="realtime-demo",
        # Keep spans fully in-process so the demo needs no backend. The runtime
        # events are delivered to the materializer via the in-process listener.
        exporter=InMemorySpanExporter(),
        api_endpoint="http://localhost:4318",
        api_key="demo",
        realtime_observability_enabled=True,
    )

    register_runtime_event_listener(_on_runtime_event)

    print("Running multi-agent workflow (live runtime events below)...\n")
    with session_start() as metadata:
        session_id = metadata["executionID"]
        plan = planner({"task": "agent observability"})
        research = researcher(plan)
        draft = writer(research)
        synthesizer({**draft, **research})

    print("\nWorkflow finished. Final materialized SessionState:\n")
    snapshot = materializer.get_snapshot(session_id)
    print(json.dumps(snapshot, indent=2))

    print(
        "\nNote: the live state above was built from pushed runtime events "
        "during execution.\nSpans for the same run are still batched and would "
        "only reach a backend ~5s later."
    )


if __name__ == "__main__":
    main()
