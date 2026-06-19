#!/usr/bin/env python3
"""Tests for hermetic managed-provider control E2E canaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CANARY = REPO_ROOT / "scripts/qa/provider-control-e2e-canary.py"


def _run_canary(
    root: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    artifact = root / "artifact.json"
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    result = subprocess.run(
        [
            sys.executable,
            str(CANARY),
            "--repo-root",
            str(REPO_ROOT),
            "--artifact",
            str(artifact),
            "--evidence-root",
            str(root / "evidence"),
            "--json",
            *args,
        ],
        cwd=REPO_ROOT,
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    return result, payload


def _write_exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_agy(path: Path, *, emit_marker: bool = True) -> Path:
    stdout_expr = "print(marker)\n" if emit_marker else "print('BASELINE_NO_HOOK')\n"
    return _write_exe(
        path,
        f"""#!/usr/bin/env python3
import json
import os
import pathlib
import re
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("1.0.3-fake")
    raise SystemExit(0)
if args[:2] == ["plugin", "install"]:
    print("installed")
    raise SystemExit(0)
if "--print" not in args:
    print("unexpected fake agy args: " + json.dumps(args), file=sys.stderr)
    raise SystemExit(2)

inbox = pathlib.Path(os.environ["LONGHOUSE_ANTIGRAVITY_INBOX_DIR"])
pending = sorted(inbox.glob("msg-*.json"))
if not pending:
    print("NO_PENDING_INPUT")
    raise SystemExit(0)
path = pending[0]
payload = json.loads(path.read_text())
claim_dir = inbox / "claimed"
claim_dir.mkdir(parents=True, exist_ok=True)
payload.update({{
    "claimed_at": "2026-01-01T00:00:00Z",
    "claimed_by": "fake-agy",
    "hook_event": "PreInvocation",
    "conversation_id": "fake-conversation",
    "step_index": "",
}})
(claim_dir / ("claimed-" + path.name)).write_text(json.dumps(payload))
path.unlink()
match = re.search(r"reply exactly ([A-Za-z0-9_]+)", payload.get("text", ""))
marker = match.group(1) if match else "MISSING_MARKER"
{stdout_expr}
""",
    )


def _fake_opencode_run(path: Path, *, emit_tool_marker: bool = True) -> Path:
    output_expr = "marker" if emit_tool_marker else "'BASELINE_NO_TOOL_MARKER'"
    return _write_exe(
        path,
        f"""#!/usr/bin/env python3
import json
import re
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("opencode 1.16.2-fake")
    raise SystemExit(0)
if not args or args[0] != "run":
    print("unexpected fake opencode args: " + json.dumps(args), file=sys.stderr)
    raise SystemExit(2)

prompt = args[-1]
match = re.search(r"printf '([^']+)'", prompt)
marker = match.group(1) if match else "MISSING_MARKER"
tool_output = {output_expr}
session_id = "ses_fake_opencode_tool"
message_id = "msg_fake_opencode_tool"
print(json.dumps({{
    "type": "tool_use",
    "timestamp": 1781861712131,
    "sessionID": session_id,
    "part": {{
        "id": "prt_fake_tool",
        "messageID": message_id,
        "sessionID": session_id,
        "type": "tool",
        "tool": "bash",
        "callID": "call_fake_tool",
        "state": {{
            "status": "completed",
            "input": {{"command": "printf '" + marker + "'"}},
            "output": tool_output,
            "metadata": {{"output": tool_output, "exit": 0, "truncated": False}},
        }},
    }},
}}))
print(json.dumps({{
    "type": "text",
    "timestamp": 1781861714070,
    "sessionID": session_id,
    "part": {{
        "id": "prt_fake_text",
        "messageID": "msg_fake_text",
        "sessionID": session_id,
        "type": "text",
        "text": "DONE",
    }},
}}))
""",
    )


def test_all_current_provider_control_paths_are_green() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        result, payload = _run_canary(Path(temp_dir), ["--provider", "all"])

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        assert set(payload["canaries"]) == {"claude", "opencode", "antigravity"}

        claude = payload["canaries"]["claude"]
        assert claude["status"] == "pass"
        assert claude["steer_meta"]["intent"] == "steer"

        opencode = payload["canaries"]["opencode"]
        assert opencode["status"] == "pass"
        assert {"serve", "session.create", "prompt_async", "abort", "attach"} <= set(opencode["observed_events"])

        antigravity = payload["canaries"]["antigravity"]
        assert antigravity["status"] == "pass"
        assert antigravity["post_injection"]["terminationBehavior"] == "force_continue"
        assert antigravity["stop_decision"]["decision"] == "continue"


def test_provider_selection_runs_one_control_lane() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        result, payload = _run_canary(Path(temp_dir), ["--provider", "opencode"])

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        assert set(payload["canaries"]) == {"opencode"}
        assert payload["canaries"]["opencode"]["status"] == "pass"


def test_antigravity_real_agy_send_canary_requires_model_visible_marker() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_home = root / "home"
        fake_bin = _fake_agy(root / "bin" / "agy", emit_marker=True)
        result, payload = _run_canary(
            root,
            [
                "--provider",
                "antigravity",
                "--antigravity-real-agy-send",
                "--antigravity-print-timeout-secs",
                "5",
            ],
            env={
                "PATH": f"{fake_bin.parent}:{os.environ['PATH']}",
                "HOME": str(fake_home),
                "LONGHOUSE_ANTIGRAVITY_BIN": str(fake_bin),
            },
        )

        assert result.returncode == 0, result.stderr + result.stdout
        agy = payload["canaries"]["antigravity"]
        assert agy["status"] == "pass"
        assert agy["operation_evidence"]["send_input"]["status"] == "pass"
        assert agy["operation_evidence"]["send_input"]["level"] == "live_token"
        assert agy["marker_in_stdout"] is True
        assert agy["baseline_in_stdout"] is False
        assert agy["matching_claim"]["hook_event"] == "PreInvocation"
        assert agy["pending_files_after"] == []


def test_antigravity_real_agy_send_canary_fails_without_injected_marker() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_home = root / "home"
        fake_bin = _fake_agy(root / "bin" / "agy", emit_marker=False)
        result, payload = _run_canary(
            root,
            [
                "--provider",
                "antigravity",
                "--antigravity-real-agy-send",
                "--antigravity-print-timeout-secs",
                "5",
            ],
            env={
                "PATH": f"{fake_bin.parent}:{os.environ['PATH']}",
                "HOME": str(fake_home),
                "LONGHOUSE_ANTIGRAVITY_BIN": str(fake_bin),
            },
        )

        assert result.returncode == 1
        agy = payload["canaries"]["antigravity"]
        assert agy["status"] == "fail"
        assert agy["failure_code"] == "antigravity_real_agy_injection_not_observed"
        assert agy["marker_in_stdout"] is False
        assert agy["baseline_in_stdout"] is True


def test_opencode_real_tool_canary_requires_completed_tool_marker() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_home = root / "home"
        fake_bin = _fake_opencode_run(root / "bin" / "opencode", emit_tool_marker=True)
        result, payload = _run_canary(
            root,
            [
                "--provider",
                "opencode",
                "--opencode-run-real-tool",
                "--opencode-run-timeout-secs",
                "5",
            ],
            env={
                "PATH": f"{fake_bin.parent}:{os.environ['PATH']}",
                "HOME": str(fake_home),
                "LONGHOUSE_OPENCODE_BIN": str(fake_bin),
            },
        )

        assert result.returncode == 0, result.stderr + result.stdout
        opencode = payload["canaries"]["opencode"]
        assert opencode["status"] == "pass"
        assert opencode["operation_evidence"]["transcript_binding"]["status"] == "pass"
        assert opencode["operation_evidence"]["transcript_binding"]["level"] == "live_token"
        assert opencode["tool_name"] == "bash"
        assert opencode["tool_call_id"] == "call_fake_tool"
        assert opencode["tool_state_status"] == "completed"
        assert opencode["tool_event_count"] == 1
        assert opencode["text_event_count"] == 1
        assert opencode["session_ids"] == ["ses_fake_opencode_tool"]
        assert opencode["matching_tool_event"]["part"]["state"]["output"].startswith(
            "LONGHOUSE_OPENCODE_TOOL_"
        )


def test_opencode_real_tool_canary_fails_without_marker_output() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_home = root / "home"
        fake_bin = _fake_opencode_run(root / "bin" / "opencode", emit_tool_marker=False)
        result, payload = _run_canary(
            root,
            [
                "--provider",
                "opencode",
                "--opencode-run-real-tool",
                "--opencode-run-timeout-secs",
                "5",
            ],
            env={
                "PATH": f"{fake_bin.parent}:{os.environ['PATH']}",
                "HOME": str(fake_home),
                "LONGHOUSE_OPENCODE_BIN": str(fake_bin),
            },
        )

        assert result.returncode == 1
        opencode = payload["canaries"]["opencode"]
        assert opencode["status"] == "fail"
        assert opencode["failure_code"] == "opencode_real_tool_shape_missing"
        assert opencode["tool_event_count"] == 1
        assert opencode["matching_tool_event"] is None


def main() -> int:
    tests = [
        test_all_current_provider_control_paths_are_green,
        test_provider_selection_runs_one_control_lane,
        test_antigravity_real_agy_send_canary_requires_model_visible_marker,
        test_antigravity_real_agy_send_canary_fails_without_injected_marker,
        test_opencode_real_tool_canary_requires_completed_tool_marker,
        test_opencode_real_tool_canary_fails_without_marker_output,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
