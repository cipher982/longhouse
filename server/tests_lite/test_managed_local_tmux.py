"""Tests for deterministic managed-local tmux command builders."""

import shlex
import shutil
import subprocess
import time

import pytest

from zerg.services.managed_local_tmux import MANAGED_LOCAL_TMUX_DEFAULT_TERMINAL
from zerg.services.managed_local_tmux import MANAGED_LOCAL_TMUX_HISTORY_LIMIT
from zerg.services.managed_local_tmux import MANAGED_LOCAL_TMUX_REMAIN_ON_EXIT
from zerg.services.managed_local_tmux import MANAGED_LOCAL_TMUX_SERVER_LABEL
from zerg.services.managed_local_tmux import MANAGED_LOCAL_TMUX_WHEEL_SCROLL_LINES
from zerg.services.managed_local_tmux import build_managed_local_conditional_zshrc_source
from zerg.services.managed_local_tmux import build_managed_local_path_export
from zerg.services.managed_local_tmux import build_managed_local_shell_prelude
from zerg.services.managed_local_tmux import build_tmux_attach_command
from zerg.services.managed_local_tmux import build_tmux_capture_command
from zerg.services.managed_local_tmux import build_tmux_current_command_command
from zerg.services.managed_local_tmux import build_tmux_has_session_command
from zerg.services.managed_local_tmux import build_tmux_kill_session_command
from zerg.services.managed_local_tmux import build_tmux_launch_command
from zerg.services.managed_local_tmux import build_tmux_paste_text_command
from zerg.services.managed_local_tmux import build_tmux_send_text_command
from zerg.services.managed_local_tmux import build_tmux_set_remain_on_exit_command
from zerg.services.managed_local_tmux import normalize_tmux_session_name


def _wrapped_inner(command: str) -> str:
    parts = shlex.split(command)
    assert parts[:2] == ["zsh", "-lc"]
    return parts[2]


def test_normalize_tmux_session_name_sanitizes_and_prefixes():
    assert normalize_tmux_session_name(" Session 123 / weird ") == "lh-Session-123-weird"


def test_normalize_tmux_session_name_rewrites_tmux_target_separator():
    assert normalize_tmux_session_name("colon:test") == "lh-colon-test"


def test_build_tmux_launch_command_wraps_cwd_and_entry_command():
    command = build_tmux_launch_command(
        session_name="lh-demo",
        cwd="/tmp/path with spaces",
        launch_command="claude --dangerously-skip-permissions",
    )

    inner = _wrapped_inner(command)
    assert build_managed_local_path_export() in inner
    assert "if ! command -v tmux >/dev/null 2>&1; then source ~/.zshrc >/dev/null 2>&1 || true; fi" in inner
    assert "command -v tmux >/dev/null 2>&1" in inner
    expected_commands = [
        f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} start-server \\; ",
        "set-option -s escape-time 0 \\; ",
        "set-option -g status off \\; ",
        "set-option -g mouse on \\; ",
        f"set-option -g default-terminal {MANAGED_LOCAL_TMUX_DEFAULT_TERMINAL} \\; ",
        "set-option -gu terminal-features \\; ",
        "set-option -as terminal-features ',*:RGB' \\; ",
        f"set-option -g history-limit {MANAGED_LOCAL_TMUX_HISTORY_LIMIT} \\; ",
        f"set-option -g remain-on-exit {MANAGED_LOCAL_TMUX_REMAIN_ON_EXIT} \\; ",
        "unbind-key -T root WheelUpPane \\; ",
        (
            'bind-key -T root WheelUpPane if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" '
            '"send-keys -M" '
            f'"copy-mode -e ; send-keys -X -N {MANAGED_LOCAL_TMUX_WHEEL_SCROLL_LINES} scroll-up" \\; '
        ),
        "unbind-key -T copy-mode WheelUpPane \\; ",
        "unbind-key -T copy-mode WheelDownPane \\; ",
        (
            "bind-key -T copy-mode WheelUpPane send-keys -X -N "
            f"{MANAGED_LOCAL_TMUX_WHEEL_SCROLL_LINES} scroll-up \\; "
        ),
        (
            "bind-key -T copy-mode WheelDownPane send-keys -X -N "
            f"{MANAGED_LOCAL_TMUX_WHEEL_SCROLL_LINES} scroll-down \\; "
        ),
        "unbind-key -T copy-mode-vi WheelUpPane \\; ",
        "unbind-key -T copy-mode-vi WheelDownPane \\; ",
        (
            "bind-key -T copy-mode-vi WheelUpPane send-keys -X -N "
            f"{MANAGED_LOCAL_TMUX_WHEEL_SCROLL_LINES} scroll-up \\; "
        ),
        (
            "bind-key -T copy-mode-vi WheelDownPane send-keys -X -N "
            f"{MANAGED_LOCAL_TMUX_WHEEL_SCROLL_LINES} scroll-down \\; "
        ),
        "new-session -d -s lh-demo -c '/tmp/path with spaces' /tmp/longhouse-managed-lh-demo.zsh",
    ]
    cursor = -1
    for expected in expected_commands:
        next_index = inner.find(expected)
        assert next_index > cursor
        cursor = next_index
    assert "cat > /tmp/longhouse-managed-lh-demo.zsh <<'__LONGHOUSE_MANAGED_LOCAL__'" in inner
    assert "exec claude --dangerously-skip-permissions" in inner


def test_build_tmux_launch_command_resets_terminal_features_before_reapplying(monkeypatch, tmp_path):
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    shell_path = shutil.which("zsh")
    if shell_path is None:
        pytest.skip("zsh is not installed")

    import zerg.services.managed_local_tmux as tmux_mod

    socket = "lh-managed-rgb-reset-test"
    monkeypatch.setattr(tmux_mod, "MANAGED_LOCAL_TMUX_SERVER_LABEL", socket)
    workspace_one = tmp_path / "workspace-one"
    workspace_two = tmp_path / "workspace-two"
    workspace_one.mkdir()
    workspace_two.mkdir()
    base = ["tmux", "-L", socket]

    try:
        subprocess.run(base + ["kill-server"], check=False, capture_output=True, text=True)
        for session_name, workspace in (
            ("lh-demo-one", workspace_one),
            ("lh-demo-two", workspace_two),
        ):
            command = tmux_mod.build_tmux_launch_command(
                session_name=session_name,
                cwd=str(workspace),
                launch_command="sleep 30",
            )
            subprocess.run(command, shell=True, executable=shell_path, check=True, capture_output=True, text=True)

        features = subprocess.run(
            base + ["show-options", "-gv", "terminal-features"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert features.splitlines().count("*:RGB") == 1
    finally:
        subprocess.run(base + ["kill-server"], check=False, capture_output=True, text=True)


def test_managed_local_root_wheel_binding_executes_cleanly_in_tmux(monkeypatch, tmp_path):
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")

    import zerg.services.managed_local_tmux as tmux_mod

    socket = "lh-managed-wheel-runtime-test"
    monkeypatch.setattr(tmux_mod, "MANAGED_LOCAL_TMUX_SERVER_LABEL", socket)
    config_path = tmp_path / "wheel-runtime.conf"
    root_binding = next(
        option
        for option in tmux_mod._managed_local_tmux_launch_options()
        if option.startswith("bind-key -T root WheelUpPane")
    )
    runtime_command = root_binding.split("WheelUpPane ", 1)[1]
    config_path.write_text(f"{runtime_command}\n", encoding="utf-8")
    base = ["tmux", "-L", socket]

    try:
        subprocess.run(base + ["kill-server"], check=False, capture_output=True, text=True)
        subprocess.run(base + ["new-session", "-d", "-s", "lh-demo", "sleep 30"], check=True, capture_output=True, text=True)
        subprocess.run(base + ["source-file", str(config_path)], check=True, capture_output=True, text=True)

        pane_in_mode = subprocess.run(
            base + ["display-message", "-p", "-t", "lh-demo", "#{pane_in_mode}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        assert pane_in_mode == "1"
    finally:
        subprocess.run(base + ["kill-server"], check=False, capture_output=True, text=True)


def test_build_tmux_launch_command_keeps_session_after_clean_exit(monkeypatch, tmp_path):
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    shell_path = shutil.which("zsh")
    if shell_path is None:
        pytest.skip("zsh is not installed")

    import zerg.services.managed_local_tmux as tmux_mod

    socket = "lh-managed-remain-on-exit-test"
    monkeypatch.setattr(tmux_mod, "MANAGED_LOCAL_TMUX_SERVER_LABEL", socket)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_name = "lh-clean-exit"
    command = tmux_mod.build_tmux_launch_command(
        session_name=session_name,
        cwd=str(workspace),
        launch_command="zsh -lc 'printf managed-local-clean-exit && exit 0'",
    )
    base = ["tmux", "-L", socket]

    try:
        subprocess.run(base + ["kill-server"], check=False, capture_output=True, text=True)
        subprocess.run(command, shell=True, executable=shell_path, check=True, capture_output=True, text=True)
        time.sleep(0.4)

        has_session = subprocess.run(
            base + ["has-session", "-t", session_name],
            check=False,
            capture_output=True,
            text=True,
        )
        assert has_session.returncode == 0

        capture = subprocess.run(
            base + ["capture-pane", "-pt", session_name, "-S", "-40"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "managed-local-clean-exit" in capture
    finally:
        subprocess.run(base + ["kill-server"], check=False, capture_output=True, text=True)


def test_build_tmux_has_session_command_targets_session():
    inner = _wrapped_inner(build_tmux_has_session_command(session_name="lh-demo"))
    assert f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} has-session -t lh-demo" in inner


def test_build_tmux_current_command_command_targets_session():
    inner = _wrapped_inner(build_tmux_current_command_command(session_name="lh-demo"))
    assert (
        f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} display-message -p -t lh-demo '#{{pane_current_command}}'" in inner
    )


def test_build_tmux_capture_command_respects_line_window():
    inner = _wrapped_inner(build_tmux_capture_command(session_name="lh-demo", lines=120))
    assert f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} capture-pane -pt lh-demo -S -120" in inner


def test_build_tmux_capture_command_exports_launch_tmux_tmpdir():
    inner = _wrapped_inner(
        build_tmux_capture_command(
            session_name="lh-demo",
            lines=120,
            tmux_tmpdir="/tmp/lh tmux",
        )
    )
    assert "export TMUX_TMPDIR='/tmp/lh tmux'" in inner


def test_build_managed_local_shell_prelude_sources_zshrc_only_when_required_commands_are_missing():
    prelude = build_managed_local_shell_prelude(require_tmux=False, required_commands=("codex", "longhouse"))

    assert build_managed_local_path_export() in prelude
    assert (
        "if ! command -v codex >/dev/null 2>&1 || ! command -v longhouse >/dev/null 2>&1; "
        "then source ~/.zshrc >/dev/null 2>&1 || true; fi"
    ) in prelude


def test_build_managed_local_conditional_zshrc_source_dedupes_required_commands():
    fallback = build_managed_local_conditional_zshrc_source(required_commands=("tmux", "tmux", "codex"))

    assert fallback == (
        "if ! command -v tmux >/dev/null 2>&1 || ! command -v codex >/dev/null 2>&1; "
        "then source ~/.zshrc >/dev/null 2>&1 || true; fi"
    )


def test_build_tmux_kill_session_command_targets_session():
    inner = _wrapped_inner(build_tmux_kill_session_command(session_name="lh-demo"))
    assert f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} kill-session -t lh-demo" in inner


def test_build_tmux_set_remain_on_exit_command_targets_session():
    inner = _wrapped_inner(build_tmux_set_remain_on_exit_command(session_name="lh-demo", mode="failed"))
    assert f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} set-option -t lh-demo remain-on-exit failed" in inner


def test_build_tmux_attach_command_targets_session():
    inner = _wrapped_inner(build_tmux_attach_command(session_name="lh-demo"))
    assert inner.endswith(f"exec tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} attach -t lh-demo")


def test_build_tmux_send_text_command_handles_multiline_reply():
    inner = _wrapped_inner(build_tmux_send_text_command(session_name="lh-demo", text="continue\nand run tests"))
    assert (
        f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} send-keys -t lh-demo -l -- continue"
        " && "
        "sleep 1"
        " && "
        f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} send-keys -t lh-demo C-m"
        " && "
        f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} send-keys -t lh-demo -l -- 'and run tests'"
        " && "
        "sleep 1"
        " && "
        f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} send-keys -t lh-demo C-m"
    ) in inner


def test_build_tmux_send_text_command_sends_enter_literal_before_keypress():
    inner = _wrapped_inner(build_tmux_send_text_command(session_name="lh-demo", text="Enter"))
    assert f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} send-keys -t lh-demo -l -- Enter" in inner
    assert "sleep 1" in inner
    assert inner.count(f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} send-keys -t lh-demo C-m") == 1


def test_build_tmux_paste_text_command_uses_named_buffer_and_bracketed_paste():
    inner = _wrapped_inner(build_tmux_paste_text_command(session_name="lh-demo", text="continue"))
    assert f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} set-buffer -b send-lh-demo continue" in inner
    assert f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} paste-buffer -dpr -b send-lh-demo -t lh-demo" in inner
    assert inner.count(f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} send-keys -t lh-demo Enter") == 1


def test_build_tmux_paste_text_command_preserves_multiline_text():
    inner = _wrapped_inner(build_tmux_paste_text_command(session_name="lh-demo", text="continue\nand run tests"))
    assert f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} set-buffer -b send-lh-demo" in inner
    assert "continue\nand run tests" in inner
    assert f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} paste-buffer -dpr -b send-lh-demo -t lh-demo" in inner
