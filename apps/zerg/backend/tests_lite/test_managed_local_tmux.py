"""Tests for deterministic managed-local tmux command builders."""

import shlex

from zerg.services.managed_local_tmux import MANAGED_LOCAL_TMUX_SERVER_LABEL
from zerg.services.managed_local_tmux import build_tmux_attach_command
from zerg.services.managed_local_tmux import build_tmux_capture_command
from zerg.services.managed_local_tmux import build_tmux_current_command_command
from zerg.services.managed_local_tmux import build_tmux_has_session_command
from zerg.services.managed_local_tmux import build_tmux_kill_session_command
from zerg.services.managed_local_tmux import build_tmux_launch_command
from zerg.services.managed_local_tmux import build_tmux_send_text_command
from zerg.services.managed_local_tmux import build_tmux_set_remain_on_exit_command
from zerg.services.managed_local_tmux import normalize_tmux_session_name
from zerg.services.managed_local_tmux import validate_managed_transport


def test_normalize_tmux_session_name_sanitizes_and_prefixes():
    assert normalize_tmux_session_name(" Session 123 / weird ") == "lh-Session-123-weird"


def test_normalize_tmux_session_name_rewrites_tmux_target_separator():
    assert normalize_tmux_session_name("colon:test") == "lh-colon-test"


def test_validate_managed_transport_accepts_tmux_and_empty():
    assert validate_managed_transport("tmux") == "tmux"
    assert validate_managed_transport(None) is None


def test_build_tmux_launch_command_wraps_cwd_and_entry_command():
    command = build_tmux_launch_command(
        session_name="lh-demo",
        cwd="/tmp/path with spaces",
        launch_command="claude-code --dangerously-skip-permissions",
    )

    parts = shlex.split(command)
    assert parts[:7] == ["tmux", "-L", MANAGED_LOCAL_TMUX_SERVER_LABEL, "new-session", "-d", "-s", "lh-demo"]
    assert parts[7] == "cd '/tmp/path with spaces' && exec claude-code --dangerously-skip-permissions"


def test_build_tmux_has_session_command_targets_session():
    assert (
        build_tmux_has_session_command(session_name="lh-demo")
        == f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} has-session -t lh-demo"
    )


def test_build_tmux_current_command_command_targets_session():
    assert (
        build_tmux_current_command_command(session_name="lh-demo")
        == f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} display-message -p -t lh-demo '#{{pane_current_command}}'"
    )


def test_build_tmux_capture_command_respects_line_window():
    assert (
        build_tmux_capture_command(session_name="lh-demo", lines=120)
        == f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} capture-pane -pt lh-demo -S -120"
    )


def test_build_tmux_kill_session_command_targets_session():
    assert (
        build_tmux_kill_session_command(session_name="lh-demo")
        == f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} kill-session -t lh-demo"
    )


def test_build_tmux_set_remain_on_exit_command_targets_session():
    assert (
        build_tmux_set_remain_on_exit_command(session_name="lh-demo", mode="failed")
        == f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} set-option -t lh-demo remain-on-exit failed"
    )


def test_build_tmux_attach_command_targets_session():
    assert (
        build_tmux_attach_command(session_name="lh-demo")
        == f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} attach -t lh-demo"
    )


def test_build_tmux_send_text_command_handles_multiline_reply():
    command = build_tmux_send_text_command(session_name="lh-demo", text="continue\nand run tests")
    assert command == (
        f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} send-keys -t lh-demo -- continue Enter"
        " && "
        f"tmux -L {MANAGED_LOCAL_TMUX_SERVER_LABEL} send-keys -t lh-demo -- 'and run tests' Enter"
    )
