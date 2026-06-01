from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def _load_module():
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "ci" / "assert-installed-build-identity.py"
    spec = importlib.util.spec_from_file_location("assert_installed_build_identity", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_accepts_matching_full_commit_and_version(monkeypatch, capsys):
    mod = _load_module()
    payload = {
        "installed_version": "0.1.17 (3b403158)",
        "build": {
            "version": "0.1.17",
            "commit": "3b40315871558fe77984c90423851d0194337923",
        },
    }

    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )

    result = mod.main(
        [
            "--expected-commit",
            "3b40315871558fe77984c90423851d0194337923",
            "--expected-version",
            "0.1.17",
        ]
    )

    assert result == 0
    assert "matches commit" in capsys.readouterr().out


def test_rejects_truncated_installed_commit(monkeypatch, capsys):
    mod = _load_module()
    payload = {
        "build": {
            "version": "0.1.17",
            "commit": "3b403158",
        }
    }

    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )

    result = mod.main(["--expected-commit", "3b40315871558fe77984c90423851d0194337923"])

    assert result == 1
    assert "commit mismatch" in capsys.readouterr().err


def test_fails_on_commit_mismatch(monkeypatch, capsys):
    mod = _load_module()
    payload = {
        "build": {
            "version": "0.1.16",
            "commit": "a1160df0704b72763ed8e5cf252d2fc2819b5e5b",
        }
    }

    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )

    result = mod.main(["--expected-commit", "3b40315871558fe77984c90423851d0194337923"])

    assert result == 1
    assert "commit mismatch" in capsys.readouterr().err


def test_fails_when_version_json_is_unavailable(monkeypatch, capsys):
    mod = _load_module()

    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout="", stderr="missing identity"),
    )

    result = mod.main(["--expected-commit", "3b403158"])

    assert result == 1
    assert "version --json failed" in capsys.readouterr().err
