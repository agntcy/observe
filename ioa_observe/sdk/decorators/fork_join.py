# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

"""
Implicit fork/join detection for fan-out / fan-in tracing.

This module automatically detects parallel execution patterns without
requiring users to annotate their code. Detection works by tracking
in-flight agent spans per session and identifying siblings that share
the same parent agent.

Detection logic:
  - FORK: When a new agent span starts and the "last agent" (its parent)
    already has another child agent in-flight, we know we have a fan-out.
    A fork_id is auto-generated and applied to all siblings.
  - JOIN: When a new agent span starts and the previous in-flight agents
    all share the same fork_id (and that fork group has no more in-flight
    members), we know we have a fan-in. The joining agent gets links to
    ALL branch spans.
"""

import logging
import threading
import uuid
from typing import List, Optional, Tuple

from opentelemetry.trace import Link, SpanContext, TraceFlags

from ioa_observe.sdk.client import kv_store
from ioa_observe.sdk.semantic_conventions.agent_communication import (
    ForkJoinAttributes,
    ForkJoinEvents,
)

logger = logging.getLogger(__name__)

# Module-level lock for fork detection state.
# This serializes the read-check-write cycle that detects siblings.
_fork_lock = threading.Lock()

# Active span registry — keeps live references to span objects so we can
# retroactively set attributes on the first fork sibling.
_active_spans: dict = {}
_active_spans_lock = threading.Lock()


def register_active_span(session_id: str, sequence: int, span) -> None:
    """Store a live span reference so it can be retroactively annotated."""
    with _active_spans_lock:
        _active_spans[f"{session_id}.{sequence}"] = span


def get_active_span(session_id: str, sequence: int):
    """Retrieve a previously registered span. Returns None if not found."""
    with _active_spans_lock:
        return _active_spans.get(f"{session_id}.{sequence}")


def unregister_active_span(session_id: str, sequence: int) -> None:
    """Remove a span reference (called on span end)."""
    with _active_spans_lock:
        _active_spans.pop(f"{session_id}.{sequence}", None)


# Tool span registry — maps "session.parent_span_hex.tool_seq" → span.
_active_tool_spans: dict = {}


def _register_tool_span(
    session_id: str, parent_span_hex: str, tool_seq: int, span
) -> None:
    with _active_spans_lock:
        _active_tool_spans[f"{session_id}.{parent_span_hex}.{tool_seq}"] = span


def _get_tool_span(session_id: str, parent_span_hex: str, tool_seq: int):
    with _active_spans_lock:
        return _active_tool_spans.get(f"{session_id}.{parent_span_hex}.{tool_seq}")


def _unregister_tool_span(session_id: str, parent_span_hex: str, tool_seq: int) -> None:
    with _active_spans_lock:
        _active_tool_spans.pop(f"{session_id}.{parent_span_hex}.{tool_seq}", None)


# ---------------------------------------------------------------------------
# kv_store key helpers
# ---------------------------------------------------------------------------


def _agents_key(session_id: str, seq: int, field: str) -> str:
    """Per-sequence agent record key."""
    return f"session.{session_id}.agents.{seq}.{field}"


def _fork_key(session_id: str, fork_id: str, field: str) -> str:
    """Fork registry key."""
    return f"session.{session_id}.fork.{fork_id}.{field}"


# ---------------------------------------------------------------------------
# Per-sequence agent registry  (write on every agent start)
# ---------------------------------------------------------------------------


def store_agent_record(
    session_id: str,
    sequence: int,
    span_id: str,
    trace_id: str,
    entity_name: str,
    parent_seq: int,
    fork_id: str = "",
    branch_index: int = -1,
) -> None:
    """Write an immutable per-sequence record for an agent invocation."""
    kv_store.set(_agents_key(session_id, sequence, "span_id"), span_id)
    kv_store.set(_agents_key(session_id, sequence, "trace_id"), trace_id)
    kv_store.set(_agents_key(session_id, sequence, "name"), entity_name)
    kv_store.set(_agents_key(session_id, sequence, "parent_seq"), str(parent_seq))
    kv_store.set(_agents_key(session_id, sequence, "fork_id"), fork_id)
    kv_store.set(_agents_key(session_id, sequence, "branch_index"), str(branch_index))
    # "in_flight" is set to "1" on start, cleared to "0" on span end
    kv_store.set(_agents_key(session_id, sequence, "in_flight"), "1")


def mark_agent_ended(session_id: str, sequence: int) -> None:
    """Mark an agent's per-sequence record as no longer in-flight."""
    kv_store.set(_agents_key(session_id, sequence, "in_flight"), "0")


def get_agent_record(session_id: str, sequence: int) -> Optional[dict]:
    """Read one per-sequence agent record. Returns None if not found."""
    span_id = kv_store.get(_agents_key(session_id, sequence, "span_id"))
    if not span_id:
        return None
    return {
        "span_id": span_id,
        "trace_id": kv_store.get(_agents_key(session_id, sequence, "trace_id")),
        "name": kv_store.get(_agents_key(session_id, sequence, "name")),
        "parent_seq": kv_store.get(_agents_key(session_id, sequence, "parent_seq")),
        "fork_id": kv_store.get(_agents_key(session_id, sequence, "fork_id")) or "",
        "branch_index": kv_store.get(_agents_key(session_id, sequence, "branch_index"))
        or "-1",
        "in_flight": kv_store.get(_agents_key(session_id, sequence, "in_flight"))
        or "0",
        "sequence": sequence,
    }


# ---------------------------------------------------------------------------
# Fork registry
# ---------------------------------------------------------------------------


def _create_fork(session_id: str, parent_seq: int) -> str:
    """Create a new fork group and return its fork_id."""
    fork_id = str(uuid.uuid4())
    kv_store.set(_fork_key(session_id, fork_id, "parent_seq"), str(parent_seq))
    kv_store.set(_fork_key(session_id, fork_id, "branches"), "")
    return fork_id


def _add_branch_to_fork(session_id: str, fork_id: str, sequence: int) -> int:
    """Append a sequence number to a fork group's branch list.
    Returns the 0-based branch index."""
    key = _fork_key(session_id, fork_id, "branches")
    existing = kv_store.get(key) or ""
    branch_list = [s for s in existing.split(",") if s]
    branch_index = len(branch_list)
    branch_list.append(str(sequence))
    kv_store.set(key, ",".join(branch_list))
    return branch_index


def get_fork_branches(session_id: str, fork_id: str) -> List[int]:
    """Return all sequence numbers belonging to a fork group."""
    key = _fork_key(session_id, fork_id, "branches")
    raw = kv_store.get(key) or ""
    return [int(s) for s in raw.split(",") if s]


def get_fork_parent_seq(session_id: str, fork_id: str) -> int:
    """Return the parent sequence of a fork group."""
    raw = kv_store.get(_fork_key(session_id, fork_id, "parent_seq"))
    return int(raw) if raw else 0


def find_parent_agent_seq(
    session_id: str, parent_span_id_hex: str, max_seq: int
) -> int:
    """Find the sequence of the agent whose span_id matches parent_span_id_hex.

    Used to determine the true logical parent agent from the OTel span
    hierarchy, rather than relying on sequential ordering.  This is critical
    for fan-out detection: parallel agents all share the same OTel parent
    (the dispatching agent), so they get the same parent_seq.

    Args:
        session_id: Current session ID.
        parent_span_id_hex: Hex-encoded span ID of the OTel parent span.
        max_seq: Upper bound (exclusive) on sequence numbers to search.

    Returns:
        The parent agent's sequence number, or 0 if not found.
    """
    for seq in range(max_seq - 1, 0, -1):
        stored = kv_store.get(_agents_key(session_id, seq, "span_id"))
        if stored == parent_span_id_hex:
            return seq
    return 0


# ---------------------------------------------------------------------------
# Implicit fork detection  (called from _store_agent_span_info)
# ---------------------------------------------------------------------------


def detect_fork_on_agent_start(
    session_id: str,
    new_sequence: int,
    parent_seq: int,
    span_id: str,
    trace_id: str,
    entity_name: str,
) -> Tuple[str, int]:
    """Detect whether this new agent is part of a fan-out.

    The rule: if there is already another agent record whose parent_seq
    equals ours AND that record is still in-flight, then we are siblings
    in a fork group.

    Returns:
        (fork_id, branch_index) — fork_id is "" if no fork detected.
    """
    with _fork_lock:
        fork_id = ""
        branch_index = -1

        # Scan existing agent records for siblings with the same parent
        existing_sibling_fork_id = ""
        for seq in range(1, new_sequence):
            rec = get_agent_record(session_id, seq)
            if rec is None:
                continue
            rec_parent = int(rec["parent_seq"]) if rec["parent_seq"] else 0
            if rec_parent == parent_seq and rec["in_flight"] == "1":
                # Found a sibling! Check if it already has a fork_id
                if rec["fork_id"]:
                    existing_sibling_fork_id = rec["fork_id"]
                else:
                    # First sibling didn't know it was a fork yet.
                    # Create a fork group and retroactively tag the first sibling.
                    existing_sibling_fork_id = _create_fork(session_id, parent_seq)
                    first_branch_idx = _add_branch_to_fork(
                        session_id, existing_sibling_fork_id, seq
                    )
                    # Update the first sibling's record
                    kv_store.set(
                        _agents_key(session_id, seq, "fork_id"),
                        existing_sibling_fork_id,
                    )
                    kv_store.set(
                        _agents_key(session_id, seq, "branch_index"),
                        str(first_branch_idx),
                    )
                    # Retroactively annotate the first sibling's live span
                    first_span = get_active_span(session_id, seq)
                    if first_span:
                        parent_rec = get_agent_record(session_id, rec_parent)
                        p_name = parent_rec["name"] if parent_rec else "unknown"
                        annotate_fork_branch(
                            first_span,
                            existing_sibling_fork_id,
                            first_branch_idx,
                            rec_parent,
                            p_name,
                        )
                break  # one sibling is enough to confirm fork

        if existing_sibling_fork_id:
            fork_id = existing_sibling_fork_id
            branch_index = _add_branch_to_fork(session_id, fork_id, new_sequence)

        # Write the per-sequence record
        store_agent_record(
            session_id=session_id,
            sequence=new_sequence,
            span_id=span_id,
            trace_id=trace_id,
            entity_name=entity_name,
            parent_seq=parent_seq,
            fork_id=fork_id,
            branch_index=branch_index,
        )

        return fork_id, branch_index


# ---------------------------------------------------------------------------
# Implicit join detection  (called from _get_previous_agent_link)
# ---------------------------------------------------------------------------


def detect_join_and_build_links(
    session_id: str,
    current_sequence: int,
) -> Tuple[List[Link], Optional[str], Optional[str]]:
    """Detect whether this new agent is a join (fan-in) point.

    The rule: look at the most recent ended agent. If it was part of
    a fork group and ALL branches of that fork are now ended, this
    agent is the join point and should link to every branch.

    Returns:
        (links, fork_id, previous_agent_name)
        - links: list of OTel Link objects (one per branch, or one for sequential)
        - fork_id: the fork being joined, or None
        - previous_agent_name: name of the single previous agent (sequential), or None
    """
    with _fork_lock:
        # Read the "last agent" cursor
        last_span_id = kv_store.get(f"session.{session_id}.last_agent_span_id")
        last_trace_id = kv_store.get(f"session.{session_id}.last_agent_trace_id")
        last_name = kv_store.get(f"session.{session_id}.last_agent_name")

        if not last_span_id or not last_trace_id:
            return [], None, None

        # Find which sequence this "last agent" corresponds to
        last_seq = _find_sequence_by_span_id(session_id, last_span_id, current_sequence)
        if last_seq is None:
            # Can't find in registry — fall back to simple sequential link
            return (
                make_single_link(
                    last_span_id, last_trace_id, last_name, "agent_handoff"
                ),
                None,
                last_name,
            )

        last_rec = get_agent_record(session_id, last_seq)
        if last_rec is None or not last_rec["fork_id"]:
            # Previous agent wasn't part of a fork → sequential link
            return (
                make_single_link(
                    last_span_id, last_trace_id, last_name, "agent_handoff"
                ),
                None,
                last_name,
            )

        # Previous agent WAS part of a fork — check if ALL branches ended
        fork_id = last_rec["fork_id"]
        branch_seqs = get_fork_branches(session_id, fork_id)
        all_ended = all(_is_agent_ended(session_id, s) for s in branch_seqs)

        if not all_ended:
            # Some branches still running — link to this one branch only
            return (
                make_single_link(
                    last_span_id, last_trace_id, last_name, "agent_handoff"
                ),
                None,
                last_name,
            )

        # All branches ended → this is a JOIN. Create links to every branch.
        links = []
        for seq in branch_seqs:
            rec = get_agent_record(session_id, seq)
            if rec and rec["span_id"] and rec["trace_id"]:
                try:
                    link_context = SpanContext(
                        trace_id=int(rec["trace_id"], 16),
                        span_id=int(rec["span_id"], 16),
                        is_remote=False,
                        trace_flags=TraceFlags(TraceFlags.SAMPLED),
                    )
                    links.append(
                        Link(
                            context=link_context,
                            attributes={
                                "link.type": "join",
                                "link.fork_id": fork_id,
                                "link.from_agent": rec["name"] or "unknown",
                            },
                        )
                    )
                except (ValueError, TypeError):
                    pass

        return links, fork_id, None


def _is_agent_ended(session_id: str, sequence: int) -> bool:
    """Check if an agent is no longer in-flight."""
    val = kv_store.get(_agents_key(session_id, sequence, "in_flight"))
    return val == "0"


def _find_sequence_by_span_id(
    session_id: str, span_id: str, max_seq: int
) -> Optional[int]:
    """Find the sequence number of an agent by its span_id."""
    for seq in range(max_seq - 1, 0, -1):  # search backwards for efficiency
        stored = kv_store.get(_agents_key(session_id, seq, "span_id"))
        if stored == span_id:
            return seq
    return None


def make_single_link(
    span_id: str, trace_id: str, agent_name: Optional[str], link_type: str
) -> List[Link]:
    """Create a single link (sequential handoff or fork_branch)."""
    try:
        link_context = SpanContext(
            trace_id=int(trace_id, 16),
            span_id=int(span_id, 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        return [
            Link(
                context=link_context,
                attributes={
                    "link.type": link_type,
                    "link.from_agent": agent_name or "unknown",
                },
            )
        ]
    except (ValueError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Span annotation helpers  (called from _setup_span after detection)
# ---------------------------------------------------------------------------


def annotate_fork_branch(
    span, fork_id: str, branch_index: int, parent_seq: int, parent_name: str
) -> None:
    """Set fork attributes on a branch span."""
    span.set_attribute(ForkJoinAttributes.FORK_ID, fork_id)
    span.set_attribute(ForkJoinAttributes.FORK_BRANCH_INDEX, branch_index)
    span.set_attribute(ForkJoinAttributes.FORK_PARENT_SEQUENCE, parent_seq)
    span.set_attribute(ForkJoinAttributes.FORK_PARENT_NAME, parent_name)


def annotate_join(span, fork_id: str, branch_count: int) -> None:
    """Set join attributes and event on a joining span."""
    span.set_attribute(ForkJoinAttributes.JOIN_FORK_ID, fork_id)
    span.set_attribute(ForkJoinAttributes.JOIN_BRANCH_COUNT, branch_count)
    span.add_event(
        ForkJoinEvents.AGENT_JOIN,
        {
            ForkJoinAttributes.JOIN_FORK_ID: fork_id,
            ForkJoinAttributes.JOIN_BRANCH_COUNT: branch_count,
        },
    )


# ===========================================================================
# Tool-level fork detection (parallel tools within one agent)
# ===========================================================================

_tool_lock = threading.Lock()


def _tool_rec_key(session_id: str, parent_hex: str, tool_seq: int, field: str) -> str:
    """Per-tool record key, scoped by the parent agent's span_id."""
    return f"session.{session_id}.tool_parent.{parent_hex}.{tool_seq}.{field}"


def _tool_seq_key(session_id: str, parent_hex: str) -> str:
    return f"session.{session_id}.tool_parent.{parent_hex}.tool_seq"


def _tool_fork_key(session_id: str, fork_id: str, field: str) -> str:
    return f"session.{session_id}.tool_fork.{fork_id}.{field}"


def detect_tool_fork_on_start(
    session_id: str,
    parent_span_hex: str,
    span,
    tool_name: str,
    span_id_hex: str,
    trace_id_hex: str,
    parent_agent_name: str = "unknown",
) -> Tuple[int, str, int]:
    """Detect whether this tool call is part of a parallel fan-out.

    Works identically to agent fork detection but scoped to tools
    that share the same parent agent span.

    Returns:
        (tool_seq, fork_id, branch_index)
    """
    with _tool_lock:
        # Increment tool sequence for this parent agent
        sk = _tool_seq_key(session_id, parent_span_hex)
        cur = kv_store.get(sk)
        tool_seq = (int(cur) + 1) if cur else 1
        kv_store.set(sk, str(tool_seq))

        fork_id = ""
        branch_index = -1
        existing_fork = ""

        # Scan for siblings with the same parent that are in-flight
        for seq in range(1, tool_seq):
            in_fl = kv_store.get(
                _tool_rec_key(session_id, parent_span_hex, seq, "in_flight")
            )
            if in_fl != "1":
                continue
            fid = kv_store.get(
                _tool_rec_key(session_id, parent_span_hex, seq, "fork_id")
            )
            if fid:
                existing_fork = fid
            else:
                # Create fork group and retroactively tag first sibling
                existing_fork = str(uuid.uuid4())
                kv_store.set(
                    _tool_fork_key(session_id, existing_fork, "parent_span"),
                    parent_span_hex,
                )
                kv_store.set(
                    _tool_fork_key(session_id, existing_fork, "branches"), str(seq)
                )
                kv_store.set(
                    _tool_rec_key(session_id, parent_span_hex, seq, "fork_id"),
                    existing_fork,
                )
                kv_store.set(
                    _tool_rec_key(session_id, parent_span_hex, seq, "branch_index"), "0"
                )
                # Retroactively annotate first sibling's span
                first_tool_span = _get_tool_span(session_id, parent_span_hex, seq)
                if first_tool_span:
                    annotate_fork_branch(
                        first_tool_span, existing_fork, 0, 0, parent_agent_name
                    )
            break

        if existing_fork:
            fork_id = existing_fork
            bk = _tool_fork_key(session_id, fork_id, "branches")
            existing = kv_store.get(bk) or ""
            bl = [s for s in existing.split(",") if s]
            branch_index = len(bl)
            bl.append(str(tool_seq))
            kv_store.set(bk, ",".join(bl))

        # Write per-tool record
        kv_store.set(
            _tool_rec_key(session_id, parent_span_hex, tool_seq, "span_id"), span_id_hex
        )
        kv_store.set(
            _tool_rec_key(session_id, parent_span_hex, tool_seq, "trace_id"),
            trace_id_hex,
        )
        kv_store.set(
            _tool_rec_key(session_id, parent_span_hex, tool_seq, "name"), tool_name
        )
        kv_store.set(
            _tool_rec_key(session_id, parent_span_hex, tool_seq, "fork_id"), fork_id
        )
        kv_store.set(
            _tool_rec_key(session_id, parent_span_hex, tool_seq, "branch_index"),
            str(branch_index),
        )
        kv_store.set(
            _tool_rec_key(session_id, parent_span_hex, tool_seq, "in_flight"), "1"
        )

        # Register span for retroactive annotation
        _register_tool_span(session_id, parent_span_hex, tool_seq, span)

        return tool_seq, fork_id, branch_index


def mark_tool_ended_for_fork(
    session_id: str, parent_span_hex: str, tool_seq: int
) -> None:
    """Mark a tool's per-sequence record as no longer in-flight."""
    kv_store.set(_tool_rec_key(session_id, parent_span_hex, tool_seq, "in_flight"), "0")
    _unregister_tool_span(session_id, parent_span_hex, tool_seq)
