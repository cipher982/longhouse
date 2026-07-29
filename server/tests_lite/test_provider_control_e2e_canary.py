from __future__ import annotations

import importlib.util
from types import ModuleType

from zerg.qa.repo_root import default_repo_root


def _load_canary() -> ModuleType:
    path = default_repo_root() / "scripts" / "qa" / "provider-control-e2e-canary.py"
    spec = importlib.util.spec_from_file_location("provider_control_e2e_canary_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_opencode_real_run_environment_passes_only_its_explicit_token(monkeypatch) -> None:
    canary = _load_canary()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-token")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-cross-boundary")

    environment = canary._opencode_real_tool_env()  # noqa: SLF001

    assert environment["OPENROUTER_API_KEY"] == "fixture-token"
    assert "UNRELATED_SECRET" not in environment


def test_opencode_qualification_model_is_stable_and_overridable(monkeypatch) -> None:
    canary = _load_canary()
    monkeypatch.delenv("LONGHOUSE_OPENCODE_QUALIFICATION_MODEL", raising=False)
    assert canary._opencode_qualification_model() == "openrouter/~openai/gpt-mini-latest"  # noqa: SLF001

    monkeypatch.setenv("LONGHOUSE_OPENCODE_QUALIFICATION_MODEL", "openrouter/fixture/model")
    assert canary._opencode_qualification_model() == "openrouter/fixture/model"  # noqa: SLF001
