# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0


def topology_dynamism(graph):
    total_edges = len(graph["edges"])
    conditional_edges = sum(1 for edge in graph["edges"] if edge["conditional"])

    if total_edges == 0:
        return 0  # Avoid division by zero

    return conditional_edges / total_edges  # Higher ratio means more dynamic topology


def determinism_score(graph):
    total_edges = len(graph["edges"])
    conditional_edges = sum(1 for edge in graph["edges"] if edge["conditional"])

    if total_edges == 0:
        return 1  # Fully deterministic if no edges exist

    return 1 - (conditional_edges / total_edges)  # Closer to 1 means more deterministic
