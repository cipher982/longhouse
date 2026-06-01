from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "ci" / "runtime-artifact-smoke.py"
    spec = importlib.util.spec_from_file_location("runtime_artifact_smoke", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runtime_identity_accepts_exact_commit_and_version(monkeypatch):
    mod = _load_module()
    sha = "3b40315871558fe77984c90423851d0194337923"

    monkeypatch.setattr(
        mod,
        "_load_runtime_identity",
        lambda component, path, launch_path: {"version": "0.1.17", "commit": sha},
    )

    identity = mod._assert_runtime_identity(
        mod.RuntimeComponent.ENGINE,
        "/tmp/longhouse-engine",
        "/tmp/longhouse-engine",
        expected_commit=sha,
        expected_version="0.1.17",
    )

    assert identity["commit"] == sha


def test_runtime_identity_rejects_truncated_commit(monkeypatch):
    mod = _load_module()
    sha = "3b40315871558fe77984c90423851d0194337923"

    monkeypatch.setattr(
        mod,
        "_load_runtime_identity",
        lambda component, path, launch_path: {"version": "0.1.17", "commit": "3b403158"},
    )

    try:
        mod._assert_runtime_identity(
            mod.RuntimeComponent.ENGINE,
            "/tmp/longhouse-engine",
            "/tmp/longhouse-engine",
            expected_commit=sha,
            expected_version="0.1.17",
        )
    except RuntimeError as exc:
        assert "commit mismatch" in str(exc)
    else:
        raise AssertionError("expected runtime identity mismatch")
