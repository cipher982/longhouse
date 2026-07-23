"""Tests for the shared managed-launch terminal UI (_launch_ui.py)."""

from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli import _launch_ui as launch_ui

_SESSION_ID = "111a5a5d-a4b5-49eb-95f7-863a69669959"

# Strings that claim remote durability / steer. Soft-fail paths must not print these.
_STEER_CLAIMS = ("Steer from anywhere",)
_DURABLE_EXIT_CLAIMS = ("safely saved in Longhouse", "hearth is banked")
_WATCH_ONLY_CLAIMS = ("Watch on your timeline",)


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


@pytest.mark.parametrize(
    ("capability", "must_include", "must_exclude"),
    [
        (
            "steerable",
            _STEER_CLAIMS,
            _WATCH_ONLY_CLAIMS + ("Local Helm", "Registering with Longhouse"),
        ),
        (
            "registering",
            ("Registering with Longhouse", "local Helm is up"),
            _STEER_CLAIMS + _WATCH_ONLY_CLAIMS,
        ),
        (
            "local_only",
            ("Local Helm", "remote steer unavailable"),
            _STEER_CLAIMS + _WATCH_ONLY_CLAIMS,
        ),
        (
            "watch",
            _WATCH_ONLY_CLAIMS,
            _STEER_CLAIMS + ("Local Helm", "Registering with Longhouse"),
        ),
    ],
)
def test_degraded_helm_launch_panel_honesty_matrix(capsys, capability, must_include, must_exclude):
    """Guard: soft-fail / pending paths must not advertise remote steer."""
    launch_ui.launch_panel(
        provider_label="Cursor",
        base_url="https://david010.longhouse.ai",
        machine_name="cinder",
        session_id=_SESSION_ID,
        verbose=False,
        capability=capability,
    )
    out = capsys.readouterr().out
    for needle in must_include:
        assert needle in out, f"capability={capability} missing {needle!r}"
    for needle in must_exclude:
        assert needle not in out, f"capability={capability} must not claim {needle!r}"


@pytest.mark.parametrize(
    ("durable", "must_include", "must_exclude"),
    [
        (True, ("hearth is banked",), ("not synced to Longhouse",)),
        (False, ("not synced to Longhouse",), _DURABLE_EXIT_CLAIMS),
    ],
)
def test_degraded_helm_exit_bookend_honesty_matrix(capsys, durable, must_include, must_exclude):
    launch_ui.exit_bookend(exit_code=0, machine_name="cinder", durable=durable)
    out = capsys.readouterr().out
    for needle in must_include:
        assert needle in out
    for needle in must_exclude:
        assert needle not in out


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
    assert "Longhouse — Session closed" in out
    assert "hearth is banked" in out
    assert "cinder" in out
    assert "This Helm has ended." in out
    assert "thread is safely saved in Longhouse" in out
    assert "Until next time" in out
    assert "still burns" not in out
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


def test_exit_bookend_recoverable_crash_offers_continuation_without_claiming_liveness(capsys):
    launch_ui.exit_bookend(
        exit_code=7,
        machine_name="cinder",
        reattach_command="codex --resume ...",
        reattachable_on_nonzero_exit=True,
    )
    out = capsys.readouterr().out
    assert "hearth went quiet (exit 7)" in out
    assert "Continue: codex --resume ..." in out
    assert "still burns" not in out
    assert "scattered" not in out
