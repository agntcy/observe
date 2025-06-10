# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from opentelemetry.metrics import get_meter
from typing import Any, Dict, Optional
import re


def compute_agent_interpretation_score(
    sender_agent: str,
    receiver_agent: str,
    data: Any,
    expected_format: Optional[Dict] = None,
) -> float:
    """
    Compute interpretation score between agents (0.0-1.0)
    measuring if data exchanged is in correct format.

    Args:
        sender_agent: Name of the agent sending the data
        receiver_agent: Name of the agent receiving the data
        data: The data being exchanged
        expected_format: Optional schema/format definition

    Returns:
        float: Score between 0.0 (incorrect) and 1.0 (perfect)
    """
    score = 0.0
    meter = get_meter("observe.metrics")

    try:
        # Basic format checks
        if expected_format:
            # Check against expected structure
            score = _validate_against_schema(data, expected_format)
        else:
            # Perform heuristic checks if no schema provided
            score = _heuristic_format_check(data, sender_agent, receiver_agent)

        # Record the metric
        attributes = {
            "sender_agent": sender_agent,
            "receiver_agent": receiver_agent,
            "data_type": type(data).__name__,
        }

        meter.create_gauge(
            name="gen_ai.ioa.agent.interpretation_score",
            description="Score of how well agents interpret each other's data format",
            unit="1",
        ).set(score, attributes)

        return score

    except Exception as e:
        # Log error but don't disrupt execution
        print(f"Error computing interpretation score: {str(e)}")
        return 0.0


def _validate_against_schema(data: Any, schema: Dict) -> float:
    """Validate data against expected schema"""
    # Simple implementation - in production use a schema validator
    matches = 0
    total_fields = 0

    try:
        if isinstance(data, dict) and isinstance(schema, dict):
            total_fields = len(schema)
            for key, expected_type in schema.items():
                if key in data:
                    if expected_type == "str" and isinstance(data[key], str):
                        matches += 1
                    elif expected_type == "int" and isinstance(data[key], int):
                        matches += 1
                    elif expected_type == "dict" and isinstance(data[key], dict):
                        matches += 1
                    elif expected_type == "list" and isinstance(data[key], list):
                        matches += 1
                    # Add more type checks as needed
    except Exception:
        pass

    return matches / total_fields if total_fields > 0 else 0.0


def _heuristic_format_check(data: Any, sender: str, receiver: str) -> float:
    """
    Apply heuristic rules to detect if data is in expected format
    based on agent roles
    """
    # Simple implementation
    score = 0.5  # Start with neutral score

    try:
        # Check if it's a string (common for agent messages)
        if isinstance(data, str):
            # Content-based analysis instead of role-based analysis

            # Check for code/developer content
            if "```" in data:
                score += 0.2
                # More specific check for code execution results
                if (
                    "Successfully executed" in data
                    or "stdout" in data
                    or "stderr" in data
                ):
                    score += 0.2

            # Check for research/information content
            if (
                re.search(r"found \d+ results", data, re.IGNORECASE)
                or "search results" in data.lower()
            ):
                score += 0.2

            # Check for information summaries
            if (
                len(data) > 100 and "." in data and data.count(".") > 2
            ):  # Has multiple sentences
                score += 0.15

            # Check for structured data in string format (JSON-like)
            if re.search(r"\{.*:.*\}", data) or re.search(r"\[.*\]", data):
                score += 0.15

            # Check for question/response patterns
            if re.search(r"\?.*\n.*", data) or "answer:" in data.lower():
                score += 0.1

        # Check for dict-based communication
        elif isinstance(data, dict):
            # Command objects
            if "goto" in data and "update" in data:
                score = 1.0
    except Exception:
        pass

    return min(1.0, max(0.0, score))  # Ensure score is between 0 and 1
