"""
Tests to ensure no legacy status literals exist in schema definitions.

These tests prevent regression by failing if old status patterns are reintroduced.
They enforce the phase/result architecture by detecting legacy status usage.
"""

import os
import re
from pathlib import Path

import pytest


def test_no_legacy_status_literals_in_schemas():
    """Ensure no old status literals exist in schema definitions."""
    backend_path = Path(__file__).parent.parent  # Go up from tests/ to backend/

    # Combine into single regex pattern
    legacy_regex = re.compile(r'status.*=.*("(?:completed|failed|running)"|\'(?:completed|failed|running)\')')

    schema_files = []
    for root, dirs, files in os.walk(backend_path / "zerg"):
        # Skip test directories and generated files
        if "test" in root or "__pycache__" in root or ".venv" in root:
            continue

        for file in files:
            if file.endswith(".py") and ("schema" in file.lower() or "output" in file.lower()):
                schema_files.append(os.path.join(root, file))

    violations = []
    for file_path in schema_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            lines = content.split("\n")
            for line_num, line in enumerate(lines, 1):
                if legacy_regex.search(line):
                    # Skip fiche status references (different system)
                    # Skip connector test_status references (credential testing, not workflow status)
                    if "fiche" not in line.lower() and "Fiche" not in line and "test_status" not in line:
                        violations.append(f"{file_path}:{line_num} - {line.strip()}")
        except Exception as e:
            print(f"Warning: Could not read {file_path}: {e}")

    if violations:
        violation_msg = "\n".join(violations)
        pytest.fail(f"Found legacy status literals in schema files:\n{violation_msg}")


def test_all_node_outputs_use_phase_result():
    """Ensure all node executors use phase/result, not status."""
    backend_path = Path(__file__).parent.parent

    # Check node_executors.py for legacy status usage
    node_executors_path = backend_path / "zerg" / "services" / "node_executors.py"

    if not node_executors_path.exists():
        pytest.skip("node_executors.py not found")

    with open(node_executors_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Check for legacy status parameter usage in envelope creation
    legacy_patterns = [
        r'status="completed"',
        r'status="failed"',
        r'status="running"',
        r"status='completed'",
        r"status='failed'",
        r"status='running'",
    ]

    violations = []
    lines = content.split("\n")
    for line_num, line in enumerate(lines, 1):
        for pattern in legacy_patterns:
            if re.search(pattern, line):
                violations.append(f"Line {line_num}: {line.strip()}")

    if violations:
        violation_msg = "\n".join(violations)
        pytest.fail(f"Found legacy status usage in node_executors.py:\n{violation_msg}")


def test_envelope_format_uses_phase_result():
    """Test that envelope format utility functions expect phase/result fields."""
    from zerg.schemas.node_output import is_envelope_format

    # Test that is_envelope_format checks for phase, not status
    valid_envelope = {"value": "test", "meta": {"node_type": "tool", "phase": "finished", "result": "success"}}

    invalid_envelope_old_status = {
        "value": "test",
        "meta": {
            "node_type": "tool",
            "status": "completed",  # Old format
        },
    }

    # Test phase/result constraint enforcement
    invalid_finished_no_result = {
        "value": "test",
        "meta": {
            "node_type": "tool",
            "phase": "finished",
            "result": None,  # Violates constraint: finished must have result
        },
    }

    invalid_running_with_result = {
        "value": "test",
        "meta": {
            "node_type": "tool",
            "phase": "running",
            "result": "success",  # Violates constraint: only finished should have result
        },
    }

    valid_running_envelope = {
        "value": "test",
        "meta": {
            "node_type": "tool",
            "phase": "running",
            "result": None,  # Correct: running has no result
        },
    }

    assert is_envelope_format(valid_envelope), "Valid phase/result envelope should be recognized"
    assert is_envelope_format(valid_running_envelope), "Valid running envelope should be recognized"
    assert not is_envelope_format(invalid_envelope_old_status), "Old status format should be rejected"
    assert not is_envelope_format(invalid_finished_no_result), "Finished without result should be rejected"
    assert not is_envelope_format(invalid_running_with_result), "Running with result should be rejected"


def test_create_envelope_functions_signature():
    """Test that envelope creation functions use phase/result parameters."""
    import inspect

    from zerg.schemas.node_output import create_fiche_envelope
    from zerg.schemas.node_output import create_conditional_envelope
    from zerg.schemas.node_output import create_tool_envelope
    from zerg.schemas.node_output import create_trigger_envelope

    envelope_functions = [
        create_tool_envelope,
        create_fiche_envelope,
        create_conditional_envelope,
        create_trigger_envelope,
    ]

    for func in envelope_functions:
        signature = inspect.signature(func)
        params = list(signature.parameters.keys())

        # Should have phase and result parameters
        assert "phase" in params, f"{func.__name__} should have phase parameter"
        assert "result" in params, f"{func.__name__} should have result parameter"

        # Should NOT have legacy status parameter
        assert "status" not in params, f"{func.__name__} should not have legacy status parameter"

        # Should have error_message instead of error
        assert "error_message" in params, f"{func.__name__} should have error_message parameter"


def test_phase_result_constraints():
    """Test that phase/result values follow the correct constraints."""
    from zerg.schemas.node_output import create_tool_envelope

    # Valid combinations
    success_envelope = create_tool_envelope("value", phase="finished", result="success")
    assert success_envelope.meta.phase == "finished"
    assert success_envelope.meta.result == "success"

    failure_envelope = create_tool_envelope("value", phase="finished", result="failure", error_message="Test error")
    assert failure_envelope.meta.phase == "finished"
    assert failure_envelope.meta.result == "failure"
    assert failure_envelope.meta.error_message == "Test error"

    running_envelope = create_tool_envelope("value", phase="running", result=None)
    assert running_envelope.meta.phase == "running"
    assert running_envelope.meta.result is None
