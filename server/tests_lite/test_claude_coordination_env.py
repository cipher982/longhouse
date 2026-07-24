from zerg.cli.claude import _claude_subprocess_env


def test_claude_launch_does_not_inherit_parent_session_authority(monkeypatch):
    inherited = {
        "LONGHOUSE_COORDINATION_TOKEN": "parent-coordination-token",
        "LONGHOUSE_MANAGED_SESSION_ID": "parent-session",
        "LONGHOUSE_SESSION_ID": "parent-session",
        "LONGHOUSE_CHANNEL_SESSION_ID": "parent-session",
        "LONGHOUSE_PROVIDER_SESSION_ID": "parent-provider-session",
        "LONGHOUSE_RUN_ID": "parent-run",
        "LONGHOUSE_HOOK_TOKEN": "parent-hook-token",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)

    child_env = _claude_subprocess_env()

    assert inherited.keys().isdisjoint(child_env)
