from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from zerg.cli import cursor


def test_decode_subcommand_is_not_swallowed_by_helm_callback(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "missing-store.db"
    helm_calls: list[dict] = []
    monkeypatch.setattr("zerg.cli.cursor_helm.run_helm", lambda **kwargs: helm_calls.append(kwargs))

    result = CliRunner().invoke(cursor.app, ["decode", str(store), "--json"])

    assert result.exit_code == 1
    assert "unsupported_gap=" in result.output
    assert helm_calls == []


def test_unknown_options_after_separator_are_forwarded_to_cursor(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr("zerg.cli.cursor_helm.run_helm", lambda **kwargs: calls.append(kwargs))

    result = CliRunner().invoke(cursor.app, ["--", "--model", "gpt-5.3-codex-low", "hello"])

    assert result.exit_code == 0
    assert calls[0]["cursor_args"] == ["--model", "gpt-5.3-codex-low", "hello"]
    assert calls[0]["permission_policy"] == "auto_approve"
    assert calls[0]["permission_policy_explicit"] is False


def test_cursor_helm_permission_policy_flags_are_explicit(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr("zerg.cli.cursor_helm.run_helm", lambda **kwargs: calls.append(kwargs))

    remote = CliRunner().invoke(cursor.app, ["--remote-approve"])
    automatic = CliRunner().invoke(cursor.app, ["--permission-policy", "auto_approve"])
    legacy_local = CliRunner().invoke(cursor.app, ["--permission-mode", "bypass"])

    assert remote.exit_code == automatic.exit_code == legacy_local.exit_code == 0
    assert [call["permission_policy"] for call in calls] == [
        "remote_human",
        "auto_approve",
        "provider_local",
    ]
    assert all(call["permission_policy_explicit"] is True for call in calls)


def test_cursor_helm_rejects_conflicting_remote_approval_flags(monkeypatch) -> None:
    monkeypatch.setattr("zerg.cli.cursor_helm.run_helm", lambda **_kwargs: None)

    result = CliRunner().invoke(
        cursor.app,
        ["--permission-policy", "provider_local", "--remote-approve"],
    )

    assert result.exit_code == 2
    assert "conflicts" in result.output
