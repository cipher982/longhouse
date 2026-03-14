from types import SimpleNamespace
from unittest.mock import patch

from zerg.tools.builtin import runner_tools
from zerg.utils.time import utc_now_naive


class _FakeDb:
    def close(self):
        pass


class _FakeDispatcher:
    def __init__(self):
        self.calls = []

    async def dispatch_job(
        self,
        *,
        db,
        owner_id,
        runner_id,
        command,
        timeout_secs,
        commis_id,
        run_id,
    ):
        self.calls.append(
            {
                "db": db,
                "owner_id": owner_id,
                "runner_id": runner_id,
                "command": command,
                "timeout_secs": timeout_secs,
                "commis_id": commis_id,
                "run_id": run_id,
            }
        )
        return {
            "ok": True,
            "data": {
                "exit_code": 0,
                "stdout": "/Users/davidrose\n",
                "stderr": "",
                "duration_ms": 21,
            },
        }


def test_runner_exec_uses_credential_resolver_when_no_commis_context():
    fake_db = _FakeDb()
    dispatcher = _FakeDispatcher()
    runner = SimpleNamespace(
        status="online",
        capabilities=["exec.full"],
        last_seen_at=utc_now_naive(),
        runner_metadata={"capabilities": ["exec.full"]},
    )

    with (
        patch("zerg.tools.builtin.runner_tools.get_commis_context", return_value=None),
        patch(
            "zerg.tools.builtin.runner_tools.get_credential_resolver",
            return_value=SimpleNamespace(owner_id=42),
        ),
        patch("zerg.tools.builtin.runner_tools._resolve_target", return_value=(7, "cinder")),
        patch("zerg.tools.builtin.runner_tools.get_settings", return_value=SimpleNamespace(environment="dev")),
        patch("zerg.tools.builtin.runner_tools.get_db", return_value=iter([fake_db])),
        patch("zerg.tools.builtin.runner_tools.runner_crud.get_runner", return_value=runner),
        patch(
            "zerg.tools.builtin.runner_tools.get_runner_connection_manager",
            return_value=SimpleNamespace(is_online=lambda owner_id, runner_id: True),
        ),
        patch("zerg.tools.builtin.runner_tools.get_runner_job_dispatcher", return_value=dispatcher),
    ):
        result = runner_tools.runner_exec("cinder", "bash -lc pwd", timeout_secs=15)

    assert result["ok"] is True
    assert result["data"]["target"] == "cinder"
    assert result["data"]["command"] == "bash -lc pwd"
    assert result["data"]["stdout"] == "/Users/davidrose\n"
    assert dispatcher.calls == [
        {
            "db": fake_db,
            "owner_id": 42,
            "runner_id": 7,
            "command": "bash -lc pwd",
            "timeout_secs": 15,
            "commis_id": None,
            "run_id": None,
        }
    ]


def test_runner_exec_requires_authenticated_context():
    with (
        patch("zerg.tools.builtin.runner_tools.get_commis_context", return_value=None),
        patch("zerg.tools.builtin.runner_tools.get_credential_resolver", return_value=None),
    ):
        result = runner_tools.runner_exec("cube", "hostname")

    assert result["ok"] is False
    assert result["error_type"] == "validation_error"
    assert "authenticated execution context" in result["user_message"]
