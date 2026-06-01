from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_module():
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "ops" / "launch-readiness.py"
    spec = importlib.util.spec_from_file_location("launch_readiness", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_latest_run_by_workflow_keeps_newest_run_id():
    mod = _load_module()

    latest = mod.latest_run_by_workflow(
        [
            {"workflowName": "Launch Gate", "databaseId": 10, "status": "completed"},
            {"workflowName": "Launch Gate", "databaseId": 12, "status": "in_progress"},
            {"workflowName": "CI", "databaseId": 11, "status": "completed"},
        ]
    )

    assert latest["Launch Gate"]["databaseId"] == 12
    assert latest["CI"]["databaseId"] == 11


def test_live_surface_requires_build_commit(monkeypatch):
    mod = _load_module()
    sha = "3b40315871558fe77984c90423851d0194337923"

    monkeypatch.setattr(
        mod,
        "fetch_json_url",
        lambda url: {"status": "ok", "build": {"commit": sha}},
    )

    check = mod.check_live_surface("demo", "https://example.test/api/health", sha)

    assert check.ok is True
    assert check.name == "live:demo"


def test_live_surface_fails_when_commit_differs(monkeypatch):
    mod = _load_module()

    monkeypatch.setattr(
        mod,
        "fetch_json_url",
        lambda url: {"status": "ok", "build": {"commit": "a1160df0704b72763ed8e5cf252d2fc2819b5e5b"}},
    )

    check = mod.check_live_surface("demo", "https://example.test/api/health", "3b403158")

    assert check.ok is False
    assert "a1160df0" in check.detail


def test_public_package_requires_version_and_commit(monkeypatch):
    mod = _load_module()
    sha = "3b40315871558fe77984c90423851d0194337923"
    payload = {"build": {"version": "0.1.17", "commit": sha}}

    monkeypatch.setattr(
        mod,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )

    check = mod.check_public_package("v0.1.17", sha)

    assert check.ok is True
    assert "version=0.1.17" in check.detail


def test_public_package_fails_on_stale_commit(monkeypatch):
    mod = _load_module()
    payload = {
        "build": {
            "version": "0.1.16",
            "commit": "a1160df0704b72763ed8e5cf252d2fc2819b5e5b",
        }
    }

    monkeypatch.setattr(
        mod,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )

    check = mod.check_public_package("v0.1.17", "3b403158")

    assert check.ok is False
    assert "a1160df0" in check.detail
