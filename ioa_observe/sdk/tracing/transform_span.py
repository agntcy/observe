# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0


def validate_transformer_rules(config):
    """
    Validates the transformer rules configuration.

    Args:
        config (dict): The configuration dictionary to validate.

    Raises:
        ValueError: If the configuration is invalid.

    Validation rules:
    - Must have RULES section containing a list of transformation rules
    - Each rule must have a 'path' field (list of strings)
    - Path length = 1: Global transformation (e.g., ["old_key"])
    - Path length > 1: Path-specific transformation
      (e.g., ["attributes", "nested_key"])
    - action_conflict must be one of: SKIP, REPLACE, DELETE
    - DELETE action should not have a 'rename' field
    """
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a dictionary")

    if "RULES" not in config:
        raise ValueError("Configuration must contain 'RULES' section")

    rules_section = config.get("RULES", [])
    if not isinstance(rules_section, list):
        raise ValueError("RULES section must be a list")

    for i, rule in enumerate(rules_section):
        if not isinstance(rule, dict):
            raise ValueError(f"Rule {i} must be a dictionary")

        # Check required fields
        if "path" not in rule:
            raise ValueError(f"Rule {i} must have 'path' field")

        if not isinstance(rule["path"], list):
            raise ValueError(f"Rule {i} 'path' must be a list")

        if not rule["path"]:
            raise ValueError(f"Rule {i} 'path' cannot be empty")

        # Check action_conflict
        action_conflict = rule.get("action_conflict", "REPLACE")
        if action_conflict not in ["SKIP", "REPLACE", "DELETE"]:
            raise ValueError(
                f"Rule {i} action_conflict must be one of: SKIP, REPLACE, DELETE"
            )

        # Check rename field for non-DELETE actions
        if action_conflict != "DELETE" and "rename" not in rule:
            raise ValueError(
                f"Rule {i} with {action_conflict} action must have 'rename' field"
            )

        # Check rename field for DELETE action
        if action_conflict == "DELETE" and "rename" in rule:
            raise ValueError(
                f"Rule {i} with DELETE action should not have 'rename' field"
            )


def transform_json_object_configurable(data, config, current_path=()):
    """
    Recursively transforms a JSON object (dict or list) based on a unified
    configuration that contains transformation rules for both global and
    path-specific key renames, along with conflict resolution strategies.

    Args:
        data (dict or list or primitive): The JSON object or part of it
                                          to transform.
        config (dict): A dictionary containing transformation rules:
                       {
                           "RULES": [
                               {
                                   "path": ["old_key"],
                                   "rename": "new_key",
                                   "action_conflict": "SKIP"|"REPLACE"|"DELETE"
                               },
                               {
                                   "path": ["attributes",
                                           "traceloop.span.kind"],
                                   "rename": "ioa_observe.span_kind",
                                   "action_conflict": "REPLACE"
                               },
                               ...
                           ]
                       }
        current_path (tuple): The current path (sequence of keys) from the
                              root to the current data element.
                              Used for path-specific lookups.

    Returns:
        dict or list or primitive: The transformed JSON object.
    """
    if isinstance(data, dict):
        new_data = {}
        for key, value in data.items():
            full_key_path = current_path + (key,)

            # Recursively transform the value first
            transformed_value = transform_json_object_configurable(
                value, config, full_key_path
            )

            # Default to no rename and 'REPLACE' conflict strategy
            target_new_key = key
            action_on_conflict = "REPLACE"
            rule_applied = False

            # Check for matching rules based on path
            rules = config.get("RULES", [])
            for rule in rules:
                rule_path = tuple(rule["path"])

                # Check if this rule applies to the current path
                if len(rule_path) == 1:
                    # Global rule: applies if the key matches
                    if rule_path[0] == key:
                        action_on_conflict = rule.get("action_conflict", "REPLACE")

                        if action_on_conflict == "DELETE":
                            # DELETE action: skip adding this key entirely
                            rule_applied = True
                            break
                        elif "rename" in rule:
                            target_new_key = rule["rename"]
                            rule_applied = True
                            break
                else:
                    # Path-specific rule: applies if full path matches
                    if full_key_path == rule_path:
                        action_on_conflict = rule.get("action_conflict", "REPLACE")

                        if action_on_conflict == "DELETE":
                            # DELETE action: skip adding this key entirely
                            rule_applied = True
                            break
                        elif "rename" in rule:
                            target_new_key = rule["rename"]
                            rule_applied = True
                            break

            # Skip to next iteration if DELETE action was applied
            if rule_applied and action_on_conflict == "DELETE":
                continue

            # Now, decide which key to use in new_data based on rules and
            # conflict strategy
            if rule_applied and target_new_key != key:
                # A rename was proposed
                if action_on_conflict == "SKIP" and (
                    target_new_key in data or target_new_key in new_data
                ):
                    # If target key already exists in original data OR new_data
                    # AND action is SKIP, keep the original key and its
                    # transformed value. The rename is effectively "skipped".
                    new_data[key] = transformed_value
                else:
                    # If action is REPLACE, or target key doesn't exist,
                    # then perform the rename.
                    # This will either add a new key or overwrite an
                    # existing one.
                    new_data[target_new_key] = transformed_value
            else:
                # No rule applied or DELETE handled above
                new_data[key] = transformed_value

        return new_data
    elif isinstance(data, list):
        new_list = []
        for item in data:
            # For list items, the path context usually doesn't change
            # for the elements themselves
            new_list.append(
                transform_json_object_configurable(item, config, current_path)
            )
        return new_list
    else:
        # Base case: primitive types (str, int, float, bool, None)
        # are returned as is
        return data


def transform_list_of_json_objects_configurable(json_objects_list, config):
    """
    Transforms a list of JSON objects by applying key replacements based on
    the provided unified configuration.

    Args:
        json_objects_list (list): A list of Python dictionary objects
                                  (parsed JSON).
        config (dict): The unified configuration dictionary for
                       transformations.

    Returns:
        list: A new list containing the transformed JSON objects.
    """
    if not isinstance(json_objects_list, list):
        raise TypeError("Input must be a list of JSON objects.")

    transformed_list = [
        transform_json_object_configurable(obj, config) for obj in json_objects_list
    ]
    return transformed_list
