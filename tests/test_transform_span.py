# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from opentelemetry.context import get_value, attach, Context
from ioa_observe.sdk.tracing.tracing import session_start
from ioa_observe.sdk.tracing.transform_span import (
    transform_json_object_configurable,
    validate_transformer_rules,
)


class TestTransformSpan(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures."""
        self.test_rules = {
            "RULES": [
                {
                    "path": ["customsdk.span.kind"],
                    "rename": "ioa_observe.span_kind",
                    "action_conflict": "SKIP",
                },
                {
                    "path": ["customsdk.entity.name"],
                    "rename": "ioa_observe.name",
                    "action_conflict": "REPLACE",
                },
                {
                    "path": ["attributes", "customsdk.entity.input"],
                    "rename": "ioa_observe.input",
                    "action_conflict": "REPLACE",
                },
                {
                    "path": ["details", "nested_data", "customsdk.nested.id"],
                    "rename": "ioa_observe.nested_id_renamed_by_path",
                    "action_conflict": "SKIP",
                },
            ]
        }

    def test_transform_json_object_global_rename(self):
        """Test global key rename functionality."""
        data = {
            "customsdk.span.kind": "LLM",
            "customsdk.entity.name": "test_entity",
            "other_key": "unchanged",
        }

        result = transform_json_object_configurable(data, self.test_rules)

        # customsdk.span.kind should be renamed to ioa_observe.span_kind
        self.assertIn("ioa_observe.span_kind", result)
        self.assertEqual(result["ioa_observe.span_kind"], "LLM")
        self.assertNotIn("customsdk.span.kind", result)

        # customsdk.entity.name should be renamed to ioa_observe.name
        self.assertIn("ioa_observe.name", result)
        self.assertEqual(result["ioa_observe.name"], "test_entity")
        self.assertNotIn("customsdk.entity.name", result)

        # other_key should remain unchanged
        self.assertIn("other_key", result)
        self.assertEqual(result["other_key"], "unchanged")

    def test_transform_json_object_path_specific_rename(self):
        """Test path-specific key rename functionality."""
        data = {
            "attributes": {
                "customsdk.entity.input": "test_input",
                "other_attr": "unchanged",
            },
            "details": {
                "nested_data": {
                    "customsdk.nested.id": "nested_value",
                },
            },
        }

        result = transform_json_object_configurable(data, self.test_rules)

        # Path-specific rename: attributes.customsdk.entity.input
        self.assertIn("ioa_observe.input", result["attributes"])
        self.assertEqual(result["attributes"]["ioa_observe.input"], "test_input")
        self.assertNotIn("customsdk.entity.input", result["attributes"])

        # Path-specific rename: details.nested_data.customsdk.nested.id
        nested_data = result["details"]["nested_data"]
        self.assertIn("ioa_observe.nested_id_renamed_by_path", nested_data)
        self.assertEqual(
            nested_data["ioa_observe.nested_id_renamed_by_path"], "nested_value"
        )
        self.assertNotIn("customsdk.nested.id", result["details"]["nested_data"])

    def test_transform_json_object_conflict_skip(self):
        """Test SKIP action when target key already exists."""
        rules = {
            "RULES": [
                {
                    "path": ["old_key"],
                    "rename": "existing_key",
                    "action_conflict": "SKIP",
                },
            ]
        }

        # Test with existing_key processed first (should skip rename)
        data = {
            "existing_key": "existing_value",
            "old_key": "old_value",
        }

        result = transform_json_object_configurable(data, rules)

        # Since action_conflict is SKIP and existing_key is processed first,
        # old_key should remain unchanged
        self.assertIn("old_key", result)
        self.assertEqual(result["old_key"], "old_value")
        self.assertIn("existing_key", result)
        self.assertEqual(result["existing_key"], "existing_value")

    def test_transform_json_object_conflict_replace(self):
        """Test REPLACE action when target key already exists."""
        rules = {
            "RULES": [
                {
                    "path": ["old_key"],
                    "rename": "existing_key",
                    "action_conflict": "REPLACE",
                },
            ]
        }

        # Test with existing_key processed first
        data = {
            "existing_key": "existing_value",
            "old_key": "old_value",
        }

        result = transform_json_object_configurable(data, rules)

        # Since action_conflict is REPLACE, existing_key should be replaced
        self.assertNotIn("old_key", result)
        self.assertIn("existing_key", result)
        self.assertEqual(result["existing_key"], "old_value")

    @patch.dict(os.environ, {}, clear=True)
    def test_session_start_without_env_var(self):
        """Test session_start with apply_transform=True but no env variable."""
        with patch("ioa_observe.sdk.tracing.tracing.logging") as mock_logging:
            session_start(apply_transform=True)

            # Should log error and disable transformation
            mock_logging.error.assert_called_with(
                "SPAN_TRANSFORMER_RULES_FILE environment variable not set. "
                "Disabling transformation."
            )

    def test_session_start_with_invalid_file(self):
        """Test session_start with invalid transformer rules file."""
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".json"
        ) as temp_file:
            temp_file.write("invalid json content")
            temp_file_path = temp_file.name

        try:
            with patch.dict(
                os.environ, {"SPAN_TRANSFORMER_RULES_FILE": temp_file_path}
            ):
                with patch("ioa_observe.sdk.tracing.tracing.logging") as mock_logging:
                    session_start(apply_transform=True)

                    # Should log error about invalid JSON
                    mock_logging.error.assert_called()
                    error_call_args = mock_logging.error.call_args[0][0]
                    self.assertIn("Failed to load transformer rules", error_call_args)
        finally:
            os.unlink(temp_file_path)

    def test_session_start_with_valid_rules_file(self):
        """Test session_start with valid transformer rules file."""
        # Create test rules in the new unified format
        valid_test_rules = {
            "RULES": [
                {
                    "path": ["customsdk.span.kind"],
                    "rename": "ioa_observe.span_kind",
                    "action_conflict": "REPLACE",
                },
            ]
        }

        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".json"
        ) as temp_file:
            json.dump(valid_test_rules, temp_file)
            temp_file_path = temp_file.name

        try:
            with patch.dict(
                os.environ, {"SPAN_TRANSFORMER_RULES_FILE": temp_file_path}
            ):
                with patch("ioa_observe.sdk.tracing.tracing.attach"):
                    with patch(
                        "ioa_observe.sdk.tracing.tracing.set_value"
                    ) as mock_set_value:
                        session_start(apply_transform=True)

                        # Should set apply_transform to True and store rules
                        mock_set_value.assert_any_call("apply_transform", True)
                        # Also check that transformer_rules were stored
                        mock_set_value.assert_any_call(
                            "transformer_rules", valid_test_rules
                        )
        finally:
            os.unlink(temp_file_path)

    def test_session_start_with_missing_file(self):
        """Test session_start with non-existent transformer rules file."""
        non_existent_file = "/path/to/non/existent/file.json"

        with patch.dict(os.environ, {"SPAN_TRANSFORMER_RULES_FILE": non_existent_file}):
            with patch("ioa_observe.sdk.tracing.tracing.logging") as mock_logging:
                session_start(apply_transform=True)

                # Should log error about missing file
                mock_logging.error.assert_called_with(
                    f"Transformer rules file not found: "
                    f"{non_existent_file}. Disabling "
                    f"transformation."
                )

    def test_session_start_with_invalid_structure(self):
        """Test session_start with invalid transformer rules structure."""
        invalid_rules = {
            "INVALID_SECTION": {},
            "MISSING_PATH_SPECIFIC": {},
        }

        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".json"
        ) as temp_file:
            json.dump(invalid_rules, temp_file)
            temp_file_path = temp_file.name

        try:
            with patch.dict(
                os.environ, {"SPAN_TRANSFORMER_RULES_FILE": temp_file_path}
            ):
                with patch("ioa_observe.sdk.tracing.tracing.logging") as mock_logging:
                    session_start(apply_transform=True)

                    # Should log error about invalid structure
                    mock_logging.error.assert_called_with(
                        "Invalid transformer rules structure. "
                        "Expected 'RULES' section. "
                        "Disabling transformation."
                    )
        finally:
            os.unlink(temp_file_path)

    def test_action_delete(self):
        """Test DELETE action removes keys from the target object."""
        rules = {
            "RULES": [
                {
                    "path": ["user"],
                    "action_conflict": "DELETE",
                },
                {
                    "path": ["debug_info"],
                    "action_conflict": "DELETE",
                },
            ]
        }

        data = {
            "user": "sensitive_user",
            "message": "test message",
            "debug_info": "debug details",
            "status": "ok",
        }

        # Transform with DELETE actions
        result = transform_json_object_configurable(data, rules)

        # Assert deleted keys are removed
        self.assertNotIn("user", result)
        self.assertNotIn("debug_info", result)

        # Assert other keys remain
        self.assertIn("message", result)
        self.assertIn("status", result)
        self.assertEqual(result["message"], "test message")
        self.assertEqual(result["status"], "ok")

    def test_action_delete_with_conflicts(self):
        """Test DELETE action in combination with other actions."""
        rules = {
            "RULES": [
                {
                    "path": ["user"],
                    "action_conflict": "DELETE",
                },
                {
                    "path": ["status"],
                    "rename": "new_status",
                    "action_conflict": "REPLACE",
                },
            ]
        }

        data = {
            "user": "test_user",
            "status": "original",
            "message": "test",
        }

        result = transform_json_object_configurable(data, rules)

        # DELETE removes user key entirely
        self.assertNotIn("user", result)

        # REPLACE renames status to new_status
        self.assertNotIn("status", result)
        self.assertIn("new_status", result)
        self.assertEqual(result["new_status"], "original")

        # Other keys unchanged
        self.assertEqual(result["message"], "test")

    def test_validation_rules_valid(self):
        """Test validation with valid transformer rules."""
        valid_rules = {
            "RULES": [
                {
                    "path": ["old_key"],
                    "rename": "new_key",
                    "action_conflict": "REPLACE",
                },
                {
                    "path": ["test", "path"],
                    "action_conflict": "DELETE",
                },
            ]
        }

        # Should not raise any exception
        validate_transformer_rules(valid_rules)

    def test_validation_rules_invalid_structure(self):
        """Test validation with invalid rule structure."""
        invalid_rules = {
            "INVALID_SECTION": {
                "rule1": {
                    "rename": "new_key",
                },
            }
        }

        with self.assertRaises(ValueError) as context:
            validate_transformer_rules(invalid_rules)

        self.assertIn("must contain 'RULES'", str(context.exception))

    def test_validation_rules_invalid_action(self):
        """Test validation with invalid action_conflict value."""
        invalid_rules = {
            "RULES": [
                {
                    "path": ["old_key"],
                    "rename": "new_key",
                    "action_conflict": "INVALID_ACTION",
                }
            ]
        }

        with self.assertRaises(ValueError) as context:
            validate_transformer_rules(invalid_rules)

        self.assertIn("must be one of: SKIP, REPLACE, DELETE", str(context.exception))

    def test_validation_rules_delete_with_rename(self):
        """Test validation where DELETE action has rename field (invalid)."""
        invalid_rules = {
            "RULES": [
                {
                    "path": ["old_key"],
                    "rename": "new_key",
                    "action_conflict": "DELETE",
                }
            ]
        }

        with self.assertRaises(ValueError) as context:
            validate_transformer_rules(invalid_rules)

        self.assertIn(
            "DELETE action should not have 'rename' field", str(context.exception)
        )

    def test_session_start_env_variable_override_true(self):
        """Test SPAN_TRANSFORMER_RULES_ENABLED overrides
        apply_transform=False."""
        # Create a valid transformer rules file
        temp_file_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as temp_file:
                temp_file_path = temp_file.name
                rules = {
                    "RULES": [
                        {
                            "path": ["old_key"],
                            "rename": "new_key",
                            "action_conflict": "REPLACE",
                        }
                    ]
                }
                json.dump(rules, temp_file)

            # Test various true values
            for true_value in ["true", "True", "1"]:
                with self.subTest(true_value=true_value):
                    with patch.dict(
                        os.environ, {"SPAN_TRANSFORMER_RULES_FILE": temp_file_path}
                    ):
                        os.environ.update(
                            {
                                "SPAN_TRANSFORMER_RULES_FILE": temp_file_path,
                                "SPAN_TRANSFORMER_RULES_ENABLED": true_value,
                            }
                        )

                        # Clear context before test
                        attach(Context())
                        # Should be overridden
                        session_start(apply_transform=False)

                        # Check that transformation is enabled
                        self.assertTrue(get_value("apply_transform"))
                        self.assertIsNotNone(get_value("transformer_rules"))
        finally:
            if temp_file_path:
                os.unlink(temp_file_path)

    def test_session_start_env_variable_override_false(self):
        """Test SPAN_TRANSFORMER_RULES_ENABLED overrides
        apply_transform=True."""
        # Test various false values
        for false_value in ["false", "False", "0"]:
            with self.subTest(false_value=false_value):
                with patch.dict(
                    os.environ, {"SPAN_TRANSFORMER_RULES_ENABLED": false_value}
                ):
                    # Clear context before test
                    attach(Context())
                    # Should be overridden
                    session_start(apply_transform=True)

                    # Check that transformation is disabled
                    self.assertFalse(get_value("apply_transform"))

    def test_session_start_env_variable_invalid_value(self):
        """Test SPAN_TRANSFORMER_RULES_ENABLED with invalid value."""
        with patch.dict(
            os.environ, {"SPAN_TRANSFORMER_RULES_ENABLED": "invalid_value"}
        ):
            with patch("ioa_observe.sdk.tracing.tracing.logging") as mock_logging:
                # Clear context before test
                attach(Context())
                session_start(apply_transform=True)

                # Should log error and use original parameter value
                # Should be called twice:
                # 1. Invalid env var value
                # 2. SPAN_TRANSFORMER_RULES_FILE not set
                self.assertEqual(mock_logging.error.call_count, 2)

                # Check the first error message about invalid env var
                first_call = mock_logging.error.call_args_list[0][0][0]
                self.assertIn(
                    "Invalid SPAN_TRANSFORMER_RULES_ENABLED value", first_call
                )
                self.assertIn("Using parameter value: True", first_call)

                # Check the second error message about missing file
                second_call = mock_logging.error.call_args_list[1][0][0]
                self.assertIn(
                    "SPAN_TRANSFORMER_RULES_FILE environment variable not set",
                    second_call,
                )

                # Should still use the original parameter value (True)
                # but transformation gets disabled due to missing file
                self.assertFalse(get_value("apply_transform"))

    def test_parse_boolean_env_function(self):
        """Test the _parse_boolean_env helper function."""
        from ioa_observe.sdk.tracing.tracing import _parse_boolean_env

        # Test true values
        for true_val in ["true", "True", "1"]:
            with self.subTest(value=true_val):
                self.assertTrue(_parse_boolean_env(true_val))

        # Test false values
        for false_val in ["false", "False", "0"]:
            with self.subTest(value=false_val):
                self.assertFalse(_parse_boolean_env(false_val))

        # Test invalid values
        for invalid_val in ["invalid", "yes", "no", "2", ""]:
            with self.subTest(value=invalid_val):
                with self.assertRaises(ValueError) as context:
                    _parse_boolean_env(invalid_val)
                self.assertIn(
                    f"Invalid boolean value: {invalid_val}", str(context.exception)
                )

    def test_validation_path_specific_structure(self):
        """Test validation of RULES list structure."""
        # Test valid structure
        valid_rules = {
            "RULES": [
                {
                    "path": ["attributes", "test.key"],
                    "rename": "new.key",
                    "action_conflict": "REPLACE",
                },
            ]
        }
        # Should not raise any exception
        validate_transformer_rules(valid_rules)

    def test_validation_path_specific_missing_path(self):
        """Test validation when rule missing path field."""
        invalid_rules = {
            "RULES": [
                {
                    "rename": "new.key",
                    "action_conflict": "REPLACE",
                },
            ]
        }

        with self.assertRaises(ValueError) as context:
            validate_transformer_rules(invalid_rules)

        self.assertIn("must have 'path' field", str(context.exception))

    def test_validation_path_specific_invalid_path_type(self):
        """Test validation when path is not a list."""
        invalid_rules = {
            "RULES": [
                {
                    "path": "not.a.list",
                    "rename": "new.key",
                    "action_conflict": "REPLACE",
                },
            ]
        }

        with self.assertRaises(ValueError) as context:
            validate_transformer_rules(invalid_rules)

        self.assertIn("'path' must be a list", str(context.exception))

    def test_validation_path_specific_empty_path(self):
        """Test validation when path is empty."""
        invalid_rules = {
            "RULES": [
                {
                    "path": [],
                    "rename": "new.key",
                    "action_conflict": "REPLACE",
                },
            ]
        }

        with self.assertRaises(ValueError) as context:
            validate_transformer_rules(invalid_rules)

        self.assertIn("'path' cannot be empty", str(context.exception))

    def test_validation_path_specific_missing_rename_for_non_delete(self):
        """Test validation when rename field missing for non-DELETE action."""
        invalid_rules = {
            "RULES": [
                {
                    "path": ["test", "key"],
                    "action_conflict": "REPLACE",
                },
            ]
        }

        with self.assertRaises(ValueError) as context:
            validate_transformer_rules(invalid_rules)

        self.assertIn("must have 'rename' field", str(context.exception))

    def test_transform_new_path_specific_structure(self):
        """Test transformation with unified RULES structure."""
        rules = {
            "RULES": [
                {
                    "path": ["attributes", "traceloop.span.kind"],
                    "rename": "ioa_observe.span_kind",
                    "action_conflict": "REPLACE",
                },
                {
                    "path": ["metadata", "old.field"],
                    "rename": "new.field",
                    "action_conflict": "SKIP",
                },
            ]
        }

        data = {
            "attributes": {
                "traceloop.span.kind": "LLM",
                "other_attr": "keep",
            },
            "metadata": {
                "old.field": "value1",
                "new.field": "existing_value",
            },
        }

        result = transform_json_object_configurable(data, rules)

        # Check first path-specific rule (REPLACE)
        self.assertIn("ioa_observe.span_kind", result["attributes"])
        self.assertEqual(result["attributes"]["ioa_observe.span_kind"], "LLM")
        self.assertNotIn("traceloop.span.kind", result["attributes"])

        # Check second path-specific rule (SKIP because target exists)
        # With SKIP action and target exists, original key should be kept
        self.assertIn("old.field", result["metadata"])  # Original kept
        self.assertIn("new.field", result["metadata"])  # Existing kept
        self.assertEqual(result["metadata"]["new.field"], "existing_value")
        self.assertEqual(result["metadata"]["old.field"], "value1")

        # Other fields unchanged
        self.assertIn("other_attr", result["attributes"])


if __name__ == "__main__":
    unittest.main()
