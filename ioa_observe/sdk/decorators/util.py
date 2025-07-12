# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import inspect
import re
from typing import Union, Any

from llama_index.core.agent.workflow import AgentWorkflow
from llama_index.core.workflow import StopEvent, Workflow
from llama_index.core.workflow.utils import (
    get_steps_from_class,
    get_steps_from_instance,
)


def determine_workflow_type(workflow_obj: Any) -> Union[None, dict]:
    """Determines the workflow type and generates appropriate topology."""
    # Check if it's a dict mapping agent roles to agent names
    if isinstance(workflow_obj, dict) and all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in workflow_obj.items()
    ):
        return build_agent_dict_topology(workflow_obj)

    # Check if it's a list of agent names
    elif isinstance(workflow_obj, list) and all(
        isinstance(item, str) for item in workflow_obj
    ):
        return build_agent_dict_topology(workflow_obj)
    # Try LlamaIndex built-in workflow types first
    result = determine_llama_index_workflow_type(workflow_obj)
    if result:
        return result

    # Try our custom agent workflow detector
    result = detect_custom_agent_workflow(workflow_obj)
    if result:
        return result

    # For ModeratorAgent type patterns shown in agent.py
    # This special case handles the chat-based agent interaction pattern
    if (
        hasattr(workflow_obj, "invoke")
        and "chat_history" in inspect.signature(workflow_obj.invoke).parameters
    ):
        return build_chat_agent_topology(workflow_obj)

    return None


def determine_llama_index_workflow_type(workflow_obj: Any) -> Union[None, dict]:
    """Generates a graph topology dict for the llama-index compatible workflow."""
    # Check for AgentWorkflow first (more specific)
    if isinstance(workflow_obj, AgentWorkflow):
        return get_multi_agent_workflow_graph_as_json(workflow_obj)
    # Check for general Workflow (less specific)
    elif isinstance(workflow_obj, Workflow):
        return generate_topology_dict(workflow_obj)
    else:
        return None


# This function generates a graph topology dict for the workflow.
def generate_topology_dict(workflow: Workflow) -> Union[None, dict]:
    """Generates a graph topology dict for the llama-index compatible workflow."""
    if not isinstance(workflow, Workflow):
        return None

    nodes = {}
    edges = []

    steps = get_steps_from_class(workflow)
    if not steps:
        steps = get_steps_from_instance(workflow)

    # Pre-add __start__ and __end__ nodes
    nodes["__start__"] = {
        "id": "__start__",
        "name": "__start__",
        "data": "<class 'llama_index.core.workflow.StartEvent'>",
        "metadata": None,
    }
    # Get workflow class name
    workflow_class_name = workflow.__class__.__name__
    nodes[workflow_class_name] = {
        "id": workflow_class_name,
        "name": workflow_class_name,
        "data": f"Workflow: {workflow_class_name}",
        "metadata": None,
    }

    # Add edge from __start__ to workflow class
    edges.append(
        {
            "source": "__start__",
            "target": workflow_class_name,
            "data": None,
            "conditional": False,
        }
    )

    nodes["__end__"] = {
        "id": "__end__",
        "name": "__end__",
        "data": "<class 'llama_index.core.workflow.Stopvent'>",
        "metadata": None,
    }

    # Mapping to connect events to their next steps
    event_to_step = {}

    for step_name, step_func in steps.items():
        step_config = getattr(step_func, "__step_config", None)
        if step_config is None:
            continue

        # Create a node for each step
        nodes[step_name] = {
            "id": step_name,
            "name": step_name,
            "data": str(step_func),
            "metadata": None,
        }

        for event_type in step_config.accepted_events:
            event_to_step[event_type.__name__] = step_name

    # Connect __start__ to the first step(s) accepting StartEvent
    for event_name, step_name in event_to_step.items():
        if event_name == "StartEvent":
            edges.append(
                {
                    "source": workflow_class_name,
                    "target": step_name,
                    "data": None,
                    "conditional": False,
                }
            )

    # Now wire steps to their return events and onward
    for step_name, step_func in steps.items():
        step_config = getattr(step_func, "__step_config", None)
        if step_config is None:
            continue

        for return_type in step_config.return_types:
            if return_type is None:
                continue

            return_event = return_type.__name__

            # If return is StopEvent, connect to __end__
            if issubclass(return_type, StopEvent):
                edges.append(
                    {
                        "source": step_name,
                        "target": "__end__",
                        "data": None,
                        "conditional": False,
                    }
                )
            else:
                # Else, find next step that accepts this event
                next_step = event_to_step.get(return_event)
                if next_step:
                    edges.append(
                        {
                            "source": step_name,
                            "target": next_step,
                            "data": None,
                            "conditional": False,
                        }
                    )
                else:
                    # No next step? Ignore or log
                    pass
    return {"nodes": nodes, "edges": edges}


# If the agent class is utilizing AgentWorkflow and FunctionAgent, we can use this function to get the graph
def get_multi_agent_workflow_graph_as_json(agent_workflow_instance):
    nodes = {}
    edges = []

    # __start__ node
    nodes["__start__"] = {
        "id": "__start__",
        "name": "__start__",
        "data": "null",
        "metadata": None,
    }

    # __end__ node
    nodes["__end__"] = {
        "id": "__end__",
        "name": "__end__",
        "data": "null",
        "metadata": None,
    }

    # Agent nodes
    if not hasattr(agent_workflow_instance, "agents") or not hasattr(
        agent_workflow_instance, "root_agent"
    ):
        print(
            "Warning: The provided instance doesn't look like a standard AgentWorkflow with 'agents' and 'root_agent' attributes."
        )
        return {"nodes": nodes, "edges": edges}

    agent_map = agent_workflow_instance.agents  # This is {name: agent_object}

    for agent_name, agent_obj in agent_map.items():
        description = getattr(agent_obj, "description", f"Agent: {agent_name}")
        # In LlamaIndex, agent instances from AgentWorkflow.from_tools_or_functions
        # might be wrapped or be a specific kind of agent. Actual data might vary.
        # For explicitly defined agents passed to AgentWorkflow, properties like name/description are clearer.
        nodes[agent_name] = {
            "id": agent_name,
            "name": agent_name,
            "data": description,  # Or system_prompt, or str(agent_obj)
            "metadata": {"type": "agent"},
        }

    # Edge from __start__ to root_agent
    root_agent_name = agent_workflow_instance.root_agent
    if root_agent_name and root_agent_name in nodes:
        edges.append(
            {
                "source": "__start__",
                "target": root_agent_name,
                "data": "initial_handoff",
                "conditional": False,
            }
        )

    # Edges based on can_handoff_to
    for source_agent_name, source_agent_obj in agent_map.items():
        can_handoff_to_list = getattr(source_agent_obj, "can_handoff_to", [])
        if isinstance(can_handoff_to_list, list):
            for target_agent_name in can_handoff_to_list:
                if target_agent_name in nodes:  # Ensure target agent exists
                    edges.append(
                        {
                            "source": source_agent_name,
                            "target": target_agent_name,
                            "data": "potential_handoff",
                            # Handoffs are typically LLM-driven thus conditional,
                            # but this static graph shows possibility.
                            "conditional": True,  # Marking as true since actual handoff is runtime decision
                        }
                    )

        # Conceptual: If an agent doesn't hand off / can be a terminal agent in a sequence
        # This is harder to determine statically for all cases, AgentWorkflow manages termination.
        # For simplicity, we only draw explicit handoffs. An agent not handing off might lead to __end__
        # implicitly through AgentWorkflow's control logic.
        # One could add an edge to __end__ if can_handoff_to is empty and it's not the only agent.
        if (
            not can_handoff_to_list
            and len(agent_map) > 1
            and source_agent_name != root_agent_name
        ):  # very heuristic
            edges.append(
                {
                    "source": source_agent_name,
                    "target": "__end__",
                    "data": "final_step_implicit",
                    "conditional": False,
                }
            )

    # If there's only one agent, it leads to end from root.
    if len(agent_map) == 1 and root_agent_name in nodes:
        edges.append(
            {
                "source": root_agent_name,
                "target": "__end__",
                "data": "direct_completion",
                "conditional": False,
            }
        )

    return {"nodes": nodes, "edges": edges}


def detect_custom_agent_workflow(obj: Any) -> Union[None, dict]:
    # Check if object has a workflow attribute that's a LlamaIndex workflow
    if hasattr(obj, "workflow") and isinstance(obj.workflow, (Workflow, AgentWorkflow)):
        return determine_llama_index_workflow_type(obj.workflow)

    # Detects and generates topology for custom agent workflows not built with LlamaIndex."""
    # Check if this is a dictionary of agents returned from a get_agents method

    if isinstance(obj, dict) and all(
        hasattr(agent, "invoke") for agent in obj.values()
    ):
        nodes = {}
        edges = []

        # Add standard nodes
        nodes["__start__"] = {
            "id": "__start__",
            "name": "__start__",
            "data": "null",
            "metadata": None,
        }
        nodes["__end__"] = {
            "id": "__end__",
            "name": "__end__",
            "data": "null",
            "metadata": None,
        }

        # Add a workflow node to represent the overall graph
        workflow_name = "agent_workflow"
        if hasattr(obj, "__graph_name__"):
            workflow_name = obj.__graph_name__

        nodes[workflow_name] = {
            "id": workflow_name,
            "name": workflow_name,
            "data": f"Workflow: {workflow_name}",
            "metadata": {"type": "workflow"},
        }

        # Connect start to workflow
        edges.append(
            {
                "source": "__start__",
                "target": workflow_name,
                "data": "workflow_initialization",
                "conditional": False,
            }
        )

        # Add each agent as a node
        for agent_key, agent_obj in obj.items():
            agent_name = getattr(agent_obj, "name", agent_key)
            agent_class = agent_obj.__class__.__name__

            nodes[agent_key] = {
                "id": agent_key,
                "name": agent_name,
                "data": f"Agent: {agent_class}",
                "metadata": {"type": "agent"},
            }

            # Connect workflow to agent
            edges.append(
                {
                    "source": workflow_name,
                    "target": agent_key,
                    "data": "contains_agent",
                    "conditional": False,
                }
            )

            # Try to analyze agent's invoke method to find connections
            if hasattr(agent_obj, "invoke") and callable(agent_obj.invoke):
                try:
                    invoke_src = inspect.getsource(agent_obj.invoke)

                    # Look for other agent references
                    for other_key in obj.keys():
                        if other_key != agent_key and other_key in invoke_src:
                            edges.append(
                                {
                                    "source": agent_key,
                                    "target": other_key,
                                    "data": "interacts_with",
                                    "conditional": True,
                                }
                            )
                except Exception:
                    # Fall back gracefully if we can't inspect the source
                    pass

        # Connect to end (in a real workflow, this would be more specific)
        edges.append(
            {
                "source": workflow_name,
                "target": "__end__",
                "data": "workflow_completion",
                "conditional": False,
            }
        )

        return {"nodes": nodes, "edges": edges}

    # If the object itself has an invoke method, analyze that
    elif hasattr(obj, "invoke") and callable(obj.invoke):
        nodes = {}
        edges = []

        # Add the main object as a node
        main_node_name = getattr(obj, "__name__", obj.__class__.__name__)
        nodes[main_node_name] = {
            "id": main_node_name,
            "name": main_node_name,
            "data": f"Agent: {main_node_name}",
            "metadata": {"type": "agent"},
        }

        # Look for agent attributes in the object
        agent_attrs = {}
        for attr_name in dir(obj):
            attr_val = getattr(obj, attr_name)
            if (
                hasattr(attr_val, "invoke")
                and callable(attr_val.invoke)
                and not attr_name.startswith("__")
            ):
                agent_attrs[attr_name] = attr_val

        # If we found agent attributes, create nodes for them
        if agent_attrs:
            for attr_name, agent_obj in agent_attrs.items():
                agent_class = agent_obj.__class__.__name__
                nodes[attr_name] = {
                    "id": attr_name,
                    "name": attr_name,
                    "data": f"Agent: {agent_class}",
                    "metadata": {"type": "agent"},
                }

                # Connect main node to this agent
                edges.append(
                    {
                        "source": main_node_name,
                        "target": attr_name,
                        "data": "contains_agent",
                        "conditional": False,
                    }
                )

            # Try to analyze invoke method to find connections between agents
            try:
                invoke_src = inspect.getsource(obj.invoke)

                for attr_name in agent_attrs:
                    if f"{attr_name}.invoke" in invoke_src:
                        # This agent is called in the invoke method
                        edges.append(
                            {
                                "source": main_node_name,
                                "target": attr_name,
                                "data": "invokes",
                                "conditional": True,
                            }
                        )

                        # Look for interactions between agents
                        for other_name in agent_attrs:
                            if (
                                other_name != attr_name
                                and f"{other_name}" in invoke_src
                            ):
                                # Check if there's a dependency pattern
                                if (
                                    f"{attr_name}.invoke" in invoke_src
                                    and f"{other_name}.invoke" in invoke_src
                                ):
                                    result_pattern = re.search(
                                        rf"{attr_name}\.invoke.*?{other_name}\.invoke",
                                        invoke_src,
                                        re.DOTALL,
                                    )
                                    if result_pattern:
                                        edges.append(
                                            {
                                                "source": attr_name,
                                                "target": other_name,
                                                "data": "result_feeds_into",
                                                "conditional": True,
                                            }
                                        )
            except Exception:
                # Fall back gracefully if we can't inspect the source
                pass

            # If we have nodes and edges, we found a valid workflow
            if nodes and agent_attrs:
                # Add start and end nodes
                nodes["__start__"] = {
                    "id": "__start__",
                    "name": "__start__",
                    "data": "null",
                    "metadata": None,
                }
                nodes["__end__"] = {
                    "id": "__end__",
                    "name": "__end__",
                    "data": "null",
                    "metadata": None,
                }

                # Connect start to main node
                edges.append(
                    {
                        "source": "__start__",
                        "target": main_node_name,
                        "data": "workflow_start",
                        "conditional": False,
                    }
                )

                # Connect main node to end
                edges.append(
                    {
                        "source": main_node_name,
                        "target": "__end__",
                        "data": "workflow_end",
                        "conditional": False,
                    }
                )

                return {"nodes": nodes, "edges": edges}

    return None


def build_chat_agent_topology(agent_obj: Any) -> dict:
    """Build topology for chat-based agents like ModeratorAgent."""
    nodes = {}
    edges = []

    # Standard nodes
    nodes["__start__"] = {
        "id": "__start__",
        "name": "__start__",
        "data": "null",
        "metadata": None,
    }
    nodes["__end__"] = {
        "id": "__end__",
        "name": "__end__",
        "data": "null",
        "metadata": None,
    }

    # Main agent node
    agent_name = agent_obj.__class__.__name__
    nodes[agent_name] = {
        "id": agent_name,
        "name": agent_name,
        "data": f"Chat Coordinator: {agent_name}",
        "metadata": {"type": "agent"},
    }

    # Connect start to main agent
    edges.append(
        {
            "source": "__start__",
            "target": agent_name,
            "data": "initial_message",
            "conditional": False,
        }
    )

    # Look for patterns in the invoke method that suggest dynamic agent invocation
    # For example, in ModeratorAgent that manages chat agents
    try:
        invoke_src = inspect.getsource(agent_obj.invoke)

        # Check for agent list or dynamic agent loading
        if "agents_list" in invoke_src or "chat_agent_list" in invoke_src:
            # Create a dynamic agents group node
            nodes["dynamic_agents"] = {
                "id": "dynamic_agents",
                "name": "Dynamic Agents",
                "data": "Dynamically loaded chat participants",
                "metadata": {"type": "agent_group", "dynamic": True},
            }

            # Connect coordinator to dynamic agents
            edges.append(
                {
                    "source": agent_name,
                    "target": "dynamic_agents",
                    "data": "invites_to_chat",
                    "conditional": True,
                }
            )

            # Connect dynamic agents back to coordinator
            edges.append(
                {
                    "source": "dynamic_agents",
                    "target": agent_name,
                    "data": "responds_to",
                    "conditional": True,
                }
            )
    except Exception:
        # Fall back to simple representation if analysis fails
        pass

    # Connect to end
    edges.append(
        {
            "source": agent_name,
            "target": "__end__",
            "data": "conversation_end",
            "conditional": True,
        }
    )

    return {"nodes": nodes, "edges": edges}


def build_agent_dict_topology(agent_input: Union[dict, list]) -> dict:
    """
    Build a workflow topology for agent inputs.

    Supports two input formats:
    1. Dictionary mapping agent roles to agent names:
       {"moderator": "noa-moderator", "file_assistant": "noa-file-assistant"}

    2. List of agent names:
       ["noa-moderator", "noa-file-assistant", "noa-web-assistant"]

    Returns a graph topology structure representing these agents and their potential interactions.
    """
    nodes = {}
    edges = []

    # Standard nodes
    nodes["__start__"] = {
        "id": "__start__",
        "name": "__start__",
        "data": "null",
        "metadata": None,
    }
    nodes["__end__"] = {
        "id": "__end__",
        "name": "__end__",
        "data": "null",
        "metadata": None,
    }

    # Add workflow coordinator node
    workflow_name = "agent_workflow"
    nodes[workflow_name] = {
        "id": workflow_name,
        "name": workflow_name,
        "data": f"Workflow: {workflow_name}",
        "metadata": {"type": "workflow"},
    }

    # Connect start to workflow
    edges.append(
        {
            "source": "__start__",
            "target": workflow_name,
            "data": "workflow_initialization",
            "conditional": False,
        }
    )

    # Convert list input to dictionary if needed
    agent_dict = {}
    if isinstance(agent_input, list):
        for i, agent_name in enumerate(agent_input):
            # Create role names like "agent_0", "agent_1" for list inputs
            role = f"agent_{i}"
            agent_dict[role] = agent_name
    else:
        agent_dict = agent_input

    # Add each agent as a node
    for role, agent_name in agent_dict.items():
        node_id = role
        nodes[node_id] = {
            "id": node_id,
            "name": agent_name,
            "data": f"Agent: {agent_name}",
            "metadata": {"type": "agent", "role": role},
        }

        # Connect workflow to agent
        edges.append(
            {
                "source": workflow_name,
                "target": node_id,
                "data": "contains_agent",
                "conditional": False,
            }
        )

    # If "moderator" exists, assume it coordinates with other agents
    if "moderator" in agent_dict:
        for role in agent_dict:
            if role != "moderator":
                # Moderator can invoke other agents
                edges.append(
                    {
                        "source": "moderator",
                        "target": role,
                        "data": "delegates_to",
                        "conditional": True,
                    }
                )

                # Other agents report back to moderator
                edges.append(
                    {
                        "source": role,
                        "target": "moderator",
                        "data": "reports_to",
                        "conditional": True,
                    }
                )
    # Otherwise assume all agents can interact with each other
    else:
        # Create edges between all agents to represent potential interactions
        agent_roles = list(agent_dict.keys())
        for i, source_role in enumerate(agent_roles):
            for target_role in agent_roles[i + 1 :]:
                edges.append(
                    {
                        "source": source_role,
                        "target": target_role,
                        "data": "interacts_with",
                        "conditional": True,
                    }
                )
                edges.append(
                    {
                        "source": target_role,
                        "target": source_role,
                        "data": "interacts_with",
                        "conditional": True,
                    }
                )

    # Connect workflow to end
    edges.append(
        {
            "source": workflow_name,
            "target": "__end__",
            "data": "workflow_completion",
            "conditional": False,
        }
    )

    return {"nodes": nodes, "edges": edges}
