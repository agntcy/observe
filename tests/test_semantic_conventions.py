#!/usr/bin/env python3
"""
Test script to verify semantic conventions can be imported and used correctly.
"""

import sys


def test_semantic_conventions_import():
    """Test that semantic conventions module can be imported."""
    try:
        from ioa_observe.sdk.semantic_conventions import (
            AgentCommunicationAttributes,
            AgentCommunicationEvents,
            MessageType,
            StatusCode,
            set_communication_attributes,
            record_message_event,
        )

        print("✓ Successfully imported semantic conventions module")
    except ImportError as e:
        print(f"✗ Failed to import semantic conventions: {e}")
        assert False, f"Failed to import semantic conventions: {e}"


def test_attribute_constants():
    """Test that attribute constants are properly defined."""
    from ioa_observe.sdk.semantic_conventions import AgentCommunicationAttributes

    # Test a few key attributes
    assert hasattr(AgentCommunicationAttributes, "SENDER_ID")
    assert hasattr(AgentCommunicationAttributes, "RECEIVER_ID")
    assert hasattr(AgentCommunicationAttributes, "PROTOCOL")
    assert hasattr(AgentCommunicationAttributes, "MESSAGE_TYPE")
    assert hasattr(AgentCommunicationAttributes, "STATUS_CODE")

    # Verify the values follow the convention
    assert AgentCommunicationAttributes.SENDER_ID == "gen_ai.agent.sender.id"
    assert (
        AgentCommunicationAttributes.PROTOCOL
        == "gen_ai.agent.communication.protocol"
    )

    print("✓ All attribute constants are properly defined")


def test_enums():
    """Test that enum classes are properly defined."""
    from ioa_observe.sdk.semantic_conventions import MessageType, StatusCode

    # Test MessageType enum
    assert MessageType.REQUEST == "request"
    assert MessageType.RESPONSE == "response"
    assert MessageType.ERROR == "error"

    # Test StatusCode enum
    assert StatusCode.OK == "OK"
    assert StatusCode.ERROR == "ERROR"
    assert StatusCode.TIMEOUT == "TIMEOUT"

    print("✓ All enums are properly defined")


def test_helper_functions():
    """Test that helper functions exist and have correct signatures."""
    from ioa_observe.sdk.semantic_conventions import (
        calculate_message_size,
        infer_message_type,
        set_communication_attributes,
        record_message_event,
    )

    # Test calculate_message_size
    size = calculate_message_size(b"test message")
    assert size == 12

    size = calculate_message_size("test")
    assert size == 4

    size = calculate_message_size({"key": "value"})
    assert size > 0

    # Test infer_message_type
    msg_type = infer_message_type({}, operation="publish")
    assert msg_type is not None

    print("✓ All helper functions work correctly")


def test_slim_instrumentation():
    """Test that SLIM instrumentation module can be imported."""
    from ioa_observe.sdk.instrumentations.slim import SLIMInstrumentor

    # Verify the instrumentor class exists
    assert SLIMInstrumentor is not None

    print("✓ SLIM instrumentation module successfully imported")


def test_a2a_instrumentation():
    """Test that A2A instrumentation module can be imported."""
    from ioa_observe.sdk.instrumentations.a2a import A2AInstrumentor

    # Verify the instrumentor class exists
    assert A2AInstrumentor is not None

    print("✓ A2A instrumentation module successfully imported")


def main():
    """Run all tests."""
    print("Testing Semantic Conventions Implementation")
    print("=" * 60)

    tests = [
        test_semantic_conventions_import,
        test_attribute_constants,
        test_enums,
        test_helper_functions,
        test_slim_instrumentation,
        test_a2a_instrumentation,
    ]

    results = []
    for test in tests:
        print(f"\n{test.__doc__}")
        results.append(test())

    print("\n" + "=" * 60)
    passed = sum(results)
    total = len(results)

    print(f"\nTest Results: {passed}/{total} tests passed")

    if passed == total:
        print("✓ All tests passed!")
        return 0
    else:
        print("✗ Some tests failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
