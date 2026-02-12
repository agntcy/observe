# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import asyncio
import os
import time

from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import agent, tool
from ioa_observe.sdk.tracing import session_start

SERVICE_NAME = "parallel-agents-tools-demo"
Observe.init(SERVICE_NAME, api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(name="web_search")
async def web_search(query: str) -> str:
    """Simulate a web search."""
    await asyncio.sleep(0.3)  # simulate latency
    return f"[search results for '{query}': page1, page2, page3]"


@tool(name="run_code")
async def run_code(code: str) -> str:
    """Simulate code execution."""
    await asyncio.sleep(0.2)
    return f"[code output: executed '{code}' → result=42]"


@tool(name="analyze_data")
async def analyze_data(dataset: str) -> str:
    """Simulate data analysis."""
    await asyncio.sleep(0.4)
    return f"[analysis of '{dataset}': mean=3.14, std=1.41]"


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


@agent(name="researcher", description="Performs web research")
async def researcher(state: dict) -> dict:
    """Research agent — uses web_search tool."""
    query = state.get("query", "AI agents")
    results = await web_search(query)
    return {"research_results": results, "agent": "researcher"}


@agent(name="coder", description="Writes and runs code")
async def coder(state: dict) -> dict:
    """Coder agent — uses run_code tool."""
    task = state.get("code_task", "compute fibonacci(10)")
    output = await run_code(task)
    return {"code_output": output, "agent": "coder"}


@agent(name="analyst", description="Analyzes data")
async def analyst(state: dict) -> dict:
    """Analyst agent — uses analyze_data tool."""
    dataset = state.get("dataset", "quarterly_sales")
    analysis = await analyze_data(dataset)
    return {"analysis": analysis, "agent": "analyst"}


@agent(name="multi_tool_agent", description="Calls multiple tools in parallel")
async def multi_tool_agent(state: dict) -> dict:
    """Demonstrates parallel tool fan-out within a single agent."""
    search_result, code_result, analysis_result = await asyncio.gather(
        web_search(state.get("query", "AI agents")),
        run_code(state.get("code_task", "fibonacci(10)")),
        analyze_data(state.get("dataset", "quarterly_sales")),
    )
    return {
        "search": search_result,
        "code": code_result,
        "analysis": analysis_result,
        "agent": "multi_tool_agent",
    }


@agent(name="coordinator", description="Dispatches work to specialist agents")
async def coordinator(state: dict) -> dict:
    """Coordinator agent — fans out to researcher, coder, analyst in parallel."""

    results = await asyncio.gather(
        researcher(state),
        coder(state),
        analyst(state),
    )

    return {
        "branch_results": list(results),
        "dispatched_to": ["researcher", "coder", "analyst"],
    }


@agent(name="aggregator", description="Collects and summarizes parallel results")
async def aggregator(state: dict) -> dict:
    """Aggregator agent — fans in results from all branches."""
    branch_results = state.get("branch_results", [])
    summary_parts = []
    for result in branch_results:
        agent_name = result.get("agent", "unknown")
        # Pick whichever key has the result
        for key in ["research_results", "code_output", "analysis"]:
            if key in result:
                summary_parts.append(f"  {agent_name}: {result[key]}")

    summary = "Aggregated results:\n" + "\n".join(summary_parts)
    return {"summary": summary}


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------


async def run_workflow():
    """Run the full fan-out / fan-in workflow."""
    session_start()  # Start a new tracing session

    initial_state = {
        "query": "latest advances in multi-agent systems",
        "code_task": "compute prime factors of 2026",
        "dataset": "agent_performance_metrics",
    }

    # Step 1: Coordinator fans out
    print("\nCoordinator dispatching to researcher, coder, analyst...")
    t0 = time.time()
    coordinator_result = await coordinator(initial_state)
    t1 = time.time()
    print(f"    Fan-out completed in {t1 - t0:.2f}s")
    for name in coordinator_result["dispatched_to"]:
        print(f"   {name} completed")

    # Step 2: Aggregator fans in
    print("\nAggregator collecting results...")
    aggregator_result = await aggregator(coordinator_result)
    t2 = time.time()
    print(f"    Fan-in completed in {t2 - t1:.2f}s")
    print(f"\n{aggregator_result['summary']}")

    # Step 3: Multi-tool agent — parallel tool calls within one agent
    print("\nMulti-tool agent dispatching 3 tools in parallel...")
    multi_result = await multi_tool_agent(initial_state)
    t3 = time.time()
    print(f"    Parallel tool calls completed in {t3 - t2:.2f}s")
    print(f"    search: {multi_result['search'][:50]}...")
    print(f"    code:   {multi_result['code'][:50]}...")
    print(f"    analysis: {multi_result['analysis'][:50]}...")


if __name__ == "__main__":
    asyncio.run(run_workflow())
