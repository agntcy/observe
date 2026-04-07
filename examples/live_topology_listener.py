# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
from pprint import pprint

import certifi
import httpx
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import agent
from ioa_observe.sdk.tracing import get_live_topology_snapshot, session_start
from dotenv import load_dotenv

import truststore  # pip install truststore
truststore.inject_into_ssl()

load_dotenv()

research_agent_graph = None
summary_llm = None


def on_topology_event(event: dict) -> None:
    """Print a compact event line for live UI/event-bus integration."""
    event_type = event.get("type", "")

    # Per-event detail: pull the most informative sub-fields
    detail: dict = {}
    if "node" in event:
        node = event["node"]
        detail = {
            "node_id": node.get("id"),
            "kind": node.get("kind"),
            "status": node.get("status"),
            "description": node.get("description"),
        }
    elif "edge" in event:
        edge = event["edge"]
        detail = {
            "source": edge.get("source"),
            "target": edge.get("target"),
            "transport": edge.get("transport"),
            "status": edge.get("status"),
            "operation": edge.get("operation"),
        }
    elif event_type.startswith("topology.session."):
        detail = {"status": event.get("status")}
    elif event_type in ("a2a.message.sent", "a2a.message.received"):
        detail = {
            "source": event.get("source"),
            "target": event.get("target"),
            "operation": event.get("operation"),
            "message_id": event.get("message_id"),
            "protocol": event.get("protocol"),
        }

    summary = {
        "type": event_type,
        "ts_ms": event.get("timestamp_ms"),
        "session_id": event.get("session_id"),
        "trace_id": event.get("trace_id"),
        "snapshot_version": event.get("snapshot_version"),
        **{k: v for k, v in detail.items() if v is not None},
    }
    print("[topology-event]", json.dumps(summary))


Observe.init(
    "live-topology-listener-example",
    api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT", "http://localhost:4318"),
    topology_listener=on_topology_event,
)


def build_research_agent():
    llm = ChatOpenAI(model=os.getenv("LLM_MODEL", "gpt-4o-mini"))
    tavily_tool = TavilySearchResults(max_results=3)
    return create_react_agent(
        llm,
        tools=[tavily_tool],
        prompt=(
            "You are a web research agent. Use Tavily search when needed, gather "
            "up-to-date facts, and return a concise answer with sources."
        ),
    )


def get_research_agent():
    global research_agent_graph
    if research_agent_graph is None:
        research_agent_graph = build_research_agent()
    return research_agent_graph


def get_summary_llm():
    global summary_llm
    if summary_llm is None:
        summary_llm = ChatOpenAI(model=os.getenv("LLM_MODEL", "gpt-4o-mini"))
    return summary_llm


@agent(name="researcher", description="Researches a topic using Tavily search")
def researcher(query: str) -> dict:
    result = get_research_agent().invoke({"messages": [HumanMessage(content=query)]})
    final_message = result["messages"][-1]
    return {
        "query": query,
        "research_notes": final_message.content,
    }


@agent(name="briefing_writer", description="Turns research notes into a short briefing")
def briefing_writer(research_result: dict) -> dict:
    response = get_summary_llm().invoke(
        [
            SystemMessage(
                content=(
                    "You write short executive briefings. Summarize the input into "
                    "2-3 bullets and include one line about why it matters."
                )
            ),
            HumanMessage(
                content=(
                    f"Topic: {research_result['query']}\n\n"
                    f"Research notes:\n{research_result['research_notes']}"
                )
            ),
        ]
    )
    return {
        "query": research_result["query"],
        "briefing": response.content,
    }


def main() -> None:
    with session_start() as session_meta:
        query = (
            "Find one recent Tavily feature or capability and explain why it is "
            "useful for agentic research workflows."
        )
        research_result = researcher(query)
        result = briefing_writer(research_result)

        print("\nFinal result:")
        pprint(result)

        print("\nRuntime topology snapshot:")
        snapshot = get_live_topology_snapshot(session_meta["executionID"])
        pprint(snapshot)

        print(
            "\nTip: if your app also uses A2A instrumentation, this same listener "
            "will receive transport-level send/receive events in addition to the "
            "agent lifecycle events shown here."
        )


if __name__ == "__main__":
    main()



