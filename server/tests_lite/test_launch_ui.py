"""Tests for the shared managed-launch terminal UI (_launch_ui.py)."""

from __future__ import annotations

import os

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli import _launch_ui as launch_ui

_SESSION_ID = "111a5a5d-a4b5-49eb-95f7-863a69669959"


def test_display_host_strips_scheme_and_trailing_slash():
    assert launch_ui.display_host("https://david010.longhouse.ai/") == "david010.longhouse.ai"
    assert launch_ui.display_host("http://localhost:8001") == "localhost:8001"
    assert launch_ui.display_host("david010.longhouse.ai") == "david010.longhouse.ai"


def test_launch_panel_leads_with_steer_and_short_link(capsys):
    launch_ui.launch_panel(
        provider_label="Claude",
        base_url="https://david010.longhouse.ai",
        machine_name="cinder",
        session_id=_SESSION_ID,
        verbose=False,
    )
    out = capsys.readouterr().out
    assert "The hearth is lit on cinder" in out
    assert "Steer from anywhere" in out
    # Short /s/<8-hex-prefix> link, scheme stripped, no full UUID in the happy path.
    assert "david010.longhouse.ai/s/111a5a5d" in out
    assert _SESSION_ID not in out


def test_launch_panel_non_steerable_softens_to_watch(capsys):
    launch_ui.launch_panel(
        provider_label="Antigravity",
        base_url="https://david010.longhouse.ai",
        machine_name="cinder",
        session_id=_SESSION_ID,
        verbose=False,
        steerable=False,
    )
    out = capsys.readouterr().out
    assert "Watch on your timeline" in out
    assert "Steer from anywhere" not in out


def test_launch_panel_local_only_copy(capsys):
    launch_ui.launch_panel(
        provider_label="Cursor",
        base_url="https://david010.longhouse.ai",
        machine_name="cinder",
        session_id=_SESSION_ID,
        verbose=False,
        capability="local_only",
    )
    out = capsys.readouterr().out
    assert "Local Helm" in out
    assert "Steer from anywhere" not in out
    assert "Watch on your timeline" not in out


def test_exit_bookend_non_durable_clean_exit(capsys):
    launch_ui.exit_bookend(exit_code=0, machine_name="cinder", durable=False)
    out = capsys.readouterr().out
    assert "not synced to Longhouse" in out
    assert "thread saved" not in out


def test_launch_panel_verbose_appends_full_detail(capsys):
    launch_ui.launch_panel(
        provider_label="Claude",
        base_url="https://david010.longhouse.ai",
        machine_name="cinder",
        session_id=_SESSION_ID,
        verbose=True,
        attach_command="zsh -lc 'exec claude ...'",
    )
    out = capsys.readouterr().out
    assert f"Session ID: {_SESSION_ID}" in out
    assert f"https://david010.longhouse.ai/timeline/{_SESSION_ID}" in out
    assert "Attach: zsh -lc 'exec claude ...'" in out


def test_quiet_diagnostic_logs_silences_noisy_loggers_unless_verbose():
    import logging

    names = ("zerg.services.shipper.hooks", "httpx", "httpcore")
    for name in names:
        logging.getLogger(name).setLevel(logging.INFO)

    launch_ui.quiet_diagnostic_logs(verbose=False)
    for name in names:
        assert logging.getLogger(name).level == logging.WARNING

    # --verbose leaves the loggers untouched so diagnostics still flow.
    for name in names:
        logging.getLogger(name).setLevel(logging.INFO)
    launch_ui.quiet_diagnostic_logs(verbose=True)
    for name in names:
        assert logging.getLogger(name).level == logging.INFO


def test_exit_bookend_clean_exit_banks_the_hearth(capsys):
    launch_ui.exit_bookend(exit_code=0, machine_name="cinder")
    out = capsys.readouterr().out
    assert "hearth banked on cinder" in out
    assert "scattered" not in out


def test_exit_bookend_crash_scatters_and_shows_rekindle(capsys):
    launch_ui.exit_bookend(
        exit_code=1,
        machine_name="cinder",
        reattach_command='longhouse continue abc-123 "..."',
    )
    out = capsys.readouterr().out
    assert "fire scattered (exit 1)" in out
    assert "longhouse continue abc-123" in out


def test_exit_bookend_reattachable_crash_does_not_claim_death(capsys):
    # Codex leaves its bridge running on a nonzero exit — must NOT say "scattered/dead".
    launch_ui.exit_bookend(
        exit_code=7,
        machine_name="cinder",
        reattach_command="codex --resume ...",
        reattachable_on_nonzero_exit=True,
    )
    out = capsys.readouterr().out
    assert "still burns" in out
    assert "Rejoin: codex --resume ..." in out
    assert "scattered" not in out
