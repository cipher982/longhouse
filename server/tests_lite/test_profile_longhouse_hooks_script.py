from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from zerg.services.shipper.hooks import CODEX_HOOK_SCRIPT
from zerg.services.shipper.hooks import HOOK_SCRIPT


def _load_script_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "managed-local" / "profile_longhouse_hooks.py"
    spec = importlib.util.spec_from_file_location("profile_longhouse_hooks", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_hook(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text.replace("__ENGINE_PATH__", "/tmp/placeholder-engine"), encoding="utf-8")
    path.chmod(0o755)
    return path


def test_plain_outbox_scenario_creates_outbox_without_network(tmp_path):
    module = _load_script_module()
    hook_path = _write_hook(tmp_path, "longhouse-hook.sh", HOOK_SCRIPT)

    result = module.profile_provider_scenario(
        provider=module.PROVIDER_DESCRIPTORS["claude"],
        hook_source_path=hook_path,
        scenario=next(spec for spec in module.SCENARIOS if spec.name == "plain_outbox"),
        iterations=2,
    )

    assert result.provider == "claude"
    assert result.scenario == "plain_outbox"
    assert result.exit_codes == [0, 0]
    assert result.http_requests == 0
    assert result.outbox_files == 2
    assert result.engine_bind_count == 0


def test_managed_and_network_scenarios_hit_expected_branches(tmp_path):
    module = _load_script_module()
    claude_hook_path = _write_hook(tmp_path, "longhouse-hook.sh", HOOK_SCRIPT)
    codex_hook_path = _write_hook(tmp_path, "longhouse-codex-hook.sh", CODEX_HOOK_SCRIPT)

    managed_result = module.profile_provider_scenario(
        provider=module.PROVIDER_DESCRIPTORS["claude"],
        hook_source_path=claude_hook_path,
        scenario=next(spec for spec in module.SCENARIOS if spec.name == "managed_bind_outbox"),
        iterations=2,
    )
    network_result = module.profile_provider_scenario(
        provider=module.PROVIDER_DESCRIPTORS["codex"],
        hook_source_path=codex_hook_path,
        scenario=next(spec for spec in module.SCENARIOS if spec.name == "plain_network_fast"),
        iterations=2,
    )

    assert managed_result.exit_codes == [0, 0]
    assert managed_result.engine_bind_count == 2
    assert managed_result.outbox_files == 2
    assert managed_result.http_requests == 0

    assert network_result.exit_codes == [0, 0]
    assert network_result.http_requests == 2
    assert network_result.outbox_files == 0
    assert network_result.engine_bind_count == 0


def test_fresh_engine_status_forces_outbox_over_direct_post(tmp_path):
    module = _load_script_module()
    claude_hook_path = _write_hook(tmp_path, "longhouse-hook.sh", HOOK_SCRIPT)

    result = module.profile_provider_scenario(
        provider=module.PROVIDER_DESCRIPTORS["claude"],
        hook_source_path=claude_hook_path,
        scenario=next(spec for spec in module.SCENARIOS if spec.name == "managed_bind_auto_with_daemon"),
        iterations=2,
    )

    assert result.exit_codes == [0, 0]
    assert result.http_requests == 0
    assert result.outbox_files == 2
    assert result.engine_bind_count == 2
