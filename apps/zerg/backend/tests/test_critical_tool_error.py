from zerg.tools.result_utils import is_critical_tool_error


def test_runner_exec_missing_runner_is_not_critical():
    result = "{'ok': False, 'error_type': 'validation_error', 'user_message': \"Runner 'cube' not found\"}"
    assert is_critical_tool_error(result, "Runner 'cube' not found", tool_name="runner_exec") is False


def test_validation_error_is_not_critical_for_other_tools():
    result = "{'ok': False, 'error_type': 'validation_error', 'user_message': 'Invalid host format'}"
    assert is_critical_tool_error(result, "Invalid host format", tool_name="ssh_exec") is False


def test_connector_not_configured_is_critical():
    result = "{'ok': False, 'error_type': 'connector_not_configured', 'user_message': 'Slack is not connected.'}"
    assert is_critical_tool_error(result, "Slack is not connected.", tool_name="knowledge_search") is True
