from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_script_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "managed-local" / "managed_local_codex_launch_profile.py"
    spec = importlib.util.spec_from_file_location("managed_local_codex_launch_profile", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_classify_runner_command_covers_launch_phases():
    module = _load_script_module()

    assert module.classify_runner_command("printf '__LONGHOUSE_TMUX_TMPDIR__=%s\\n'") == "preflight"
    assert module.classify_runner_command("longhouse connect --hooks-only >/dev/null 2>&1") == "hooks_ensure"
    assert (
        module.classify_runner_command(
            "tmux -L longhouse-managed start-server \\; new-session -d -s lh-test -c /tmp /tmp/script.zsh"
        )
        == "tmux_launch"
    )
    assert module.classify_runner_command("tmux -L longhouse-managed has-session -t lh-test") == "tmux_has_session"
    assert (
        module.classify_runner_command("tmux -L longhouse-managed display-message -p -t lh-test '#{pane_current_command}'")
        == "tmux_display"
    )
    assert module.classify_runner_command("tmux -L longhouse-managed capture-pane -pt lh-test -S -80") == "tmux_capture"
    assert module.classify_runner_command("tmux -L longhouse-managed kill-session -t lh-test") == "tmux_kill_session"
    assert module.classify_runner_command("echo hello") == "other"


def test_parse_runner_jobs_handles_multiline_launch_commands():
    module = _load_script_module()

    log_text = """
[executor] Starting job aaa-bbb: zsh -lc 'source ~/.zshrc >/dev/null 2>&1; printf "__LONGHOUSE_TMUX_TMPDIR__=%s\\n" "${TMUX_TMPDIR:-}"'
[executor] Job aaa-bbb completed: exit_code=0, duration=2226ms, timed_out=false
[executor] Starting job ccc-ddd: zsh -lc 'source ~/.zshrc >/dev/null 2>&1; cat > /tmp/longhouse-managed-lh-zerg-1234.zsh <<'"'"'__LONGHOUSE_MANAGED_LOCAL__'"'"'
#!/bin/zsh
set -e
exec zsh -lc '"'"'exec codex --enable codex_hooks --no-alt-screen'"'"'
__LONGHOUSE_MANAGED_LOCAL__
chmod +x /tmp/longhouse-managed-lh-zerg-1234.zsh
tmux -L longhouse-managed start-server \\; new-session -d -s lh-zerg-1234 -c /tmp /tmp/longhouse-managed-lh-zerg-1234.zsh'
[executor] Job ccc-ddd completed: exit_code=0, duration=2412ms, timed_out=false
""".strip()

    jobs = module.parse_runner_jobs(log_text)

    assert len(jobs) == 2
    assert jobs[0].job_id == "aaa-bbb"
    assert jobs[0].kind == "preflight"
    assert jobs[0].duration_ms == 2226
    assert jobs[0].timed_out is False

    assert jobs[1].job_id == "ccc-ddd"
    assert jobs[1].kind == "tmux_launch"
    assert jobs[1].duration_ms == 2412
    assert "exec codex --enable codex_hooks --no-alt-screen" in jobs[1].command
    assert "tmux -L longhouse-managed start-server" in jobs[1].command


def test_extract_pane_blockers_detects_mcp_timeout_and_loading():
    module = _load_script_module()

    pane = """
╭───────────────────────────────────────╮
│ >_ OpenAI Codex (v0.116.0)            │
│                                       │
│ model:     loading   /model to change │
╰───────────────────────────────────────╯

Starting MCP servers
⚠ MCP client for `context7` timed out after 10 seconds.
⚠ MCP startup incomplete (failed: context7)
Loading conversation history
""".strip()

    blockers = module.extract_pane_blockers(pane)

    assert blockers == (
        "starting_mcp_servers",
        "loading_conversation_history",
        "model_loading",
        "mcp_startup_incomplete",
        "mcp_timeout:context7:10s",
    )


def test_filter_launch_jobs_isolates_matching_session_sequence():
    module = _load_script_module()

    jobs = (
        module.RunnerJobSample(
            job_id="prev-complete-only",
            kind="other",
            duration_ms=400,
            exit_code=0,
            timed_out=False,
            command="",
        ),
        module.RunnerJobSample(
            job_id="prev-1",
            kind="preflight",
            duration_ms=900,
            exit_code=0,
            timed_out=False,
            command='printf "__LONGHOUSE_TMUX_TMPDIR__=%s\\n"',
        ),
        module.RunnerJobSample(
            job_id="prev-2",
            kind="tmux_launch",
            duration_ms=800,
            exit_code=0,
            timed_out=False,
            command="tmux -L longhouse-managed start-server \\; new-session -d -s lh-other /tmp/old.sh",
        ),
        module.RunnerJobSample(
            job_id="prev-3",
            kind="tmux_has_session",
            duration_ms=500,
            exit_code=0,
            timed_out=False,
            command="tmux -L longhouse-managed has-session -t lh-other",
        ),
        module.RunnerJobSample(
            job_id="curr-1",
            kind="preflight",
            duration_ms=850,
            exit_code=0,
            timed_out=False,
            command='printf "__LONGHOUSE_TMUX_TMPDIR__=%s\\n"',
        ),
        module.RunnerJobSample(
            job_id="curr-2",
            kind="hooks_ensure",
            duration_ms=2600,
            exit_code=0,
            timed_out=False,
            command="longhouse connect --hooks-only >/dev/null 2>&1",
        ),
        module.RunnerJobSample(
            job_id="curr-3",
            kind="tmux_launch",
            duration_ms=820,
            exit_code=0,
            timed_out=False,
            command=(
                "cat > /tmp/longhouse-managed-lh-zerg-target.zsh\n"
                "tmux -L longhouse-managed start-server \\; new-session -d -s lh-zerg-target "
                "-c /tmp /tmp/longhouse-managed-lh-zerg-target.zsh # session-target"
            ),
        ),
        module.RunnerJobSample(
            job_id="curr-4",
            kind="tmux_has_session",
            duration_ms=560,
            exit_code=0,
            timed_out=False,
            command="tmux -L longhouse-managed has-session -t lh-zerg-target",
        ),
        module.RunnerJobSample(
            job_id="curr-5",
            kind="tmux_display",
            duration_ms=780,
            exit_code=0,
            timed_out=False,
            command="tmux -L longhouse-managed display-message -p -t lh-zerg-target '#{pane_current_command}'",
        ),
        module.RunnerJobSample(
            job_id="curr-6",
            kind="tmux_capture",
            duration_ms=590,
            exit_code=0,
            timed_out=False,
            command="tmux -L longhouse-managed capture-pane -pt lh-zerg-target -S -120",
        ),
        module.RunnerJobSample(
            job_id="curr-7",
            kind="tmux_kill_session",
            duration_ms=100,
            exit_code=0,
            timed_out=False,
            command="tmux -L longhouse-managed kill-session -t lh-zerg-target",
        ),
        module.RunnerJobSample(
            job_id="next-1",
            kind="preflight",
            duration_ms=910,
            exit_code=0,
            timed_out=False,
            command='printf "__LONGHOUSE_TMUX_TMPDIR__=%s\\n"',
        ),
        module.RunnerJobSample(
            job_id="next-2",
            kind="tmux_launch",
            duration_ms=830,
            exit_code=0,
            timed_out=False,
            command="tmux -L longhouse-managed start-server \\; new-session -d -s lh-next /tmp/new.sh",
        ),
    )

    filtered = module.filter_launch_jobs(
        jobs,
        session_name="lh-zerg-target",
        session_id="session-target",
    )

    assert [job.job_id for job in filtered] == [
        "curr-1",
        "curr-2",
        "curr-3",
        "curr-4",
        "curr-5",
        "curr-6",
    ]


def test_filter_launch_jobs_falls_back_to_session_id_match():
    module = _load_script_module()

    jobs = (
        module.RunnerJobSample(
            job_id="job-1",
            kind="hooks_ensure",
            duration_ms=2500,
            exit_code=0,
            timed_out=False,
            command="longhouse connect --hooks-only >/dev/null 2>&1",
        ),
        module.RunnerJobSample(
            job_id="job-2",
            kind="tmux_launch",
            duration_ms=800,
            exit_code=0,
            timed_out=False,
            command="tmux -L longhouse-managed start-server \\; new-session -d -s lh-random /tmp/launch.sh # sid-123",
        ),
        module.RunnerJobSample(
            job_id="job-3",
            kind="tmux_has_session",
            duration_ms=550,
            exit_code=0,
            timed_out=False,
            command="tmux -L longhouse-managed has-session -t lh-random",
        ),
    )

    filtered = module.filter_launch_jobs(
        jobs,
        session_name=None,
        session_id="sid-123",
    )

    assert [job.job_id for job in filtered] == ["job-1", "job-2", "job-3"]
