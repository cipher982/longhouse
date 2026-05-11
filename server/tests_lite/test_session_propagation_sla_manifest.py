import importlib.util
from pathlib import Path


def _load_sla_manifest_module():
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "scripts" / "ops" / "managed_profiler" / "sla_manifest.py"
    spec = importlib.util.spec_from_file_location("sla_manifest", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_session_propagation_sla_manifest_is_valid():
    sla_manifest = _load_sla_manifest_module()
    manifest = sla_manifest.load_manifest()

    assert sla_manifest.validate_manifest(manifest) == []
    assert sla_manifest.manifest_summary(manifest) == {
        "schema_version": 1,
        "cases": {"required": 1, "experimental": 7, "undefined": 2},
        "metrics": 8,
    }


def test_session_propagation_sla_manifest_keeps_codex_required_path():
    sla_manifest = _load_sla_manifest_module()
    manifest = sla_manifest.load_manifest()
    required = sla_manifest.cases_by_status(manifest, "required")

    assert [case["id"] for case in required] == ["managed_codex_warm_live_graceful_close"]
    case = required[0]
    assert case["provider"] == "codex"
    assert case["control_path"] == "managed"
    assert case["profile"] == "warm-live"
    assert "timeline_sse" in case["required_observers"]
    assert "browser_card" in case["required_observers"]


def test_session_propagation_sla_metric_aliases_support_existing_profiler_names():
    sla_manifest = _load_sla_manifest_module()
    manifest = sla_manifest.load_manifest()

    assert sla_manifest.metric_target_ms(manifest, "live_first_from_local_ms") == 500
    assert sla_manifest.metric_target_ms(manifest, "close_observed_ms") == 1000
    assert sla_manifest.metric_target_ms(manifest, "durable_archive_local_to_hosted_ms") == 3000


def test_session_propagation_sla_inventory_is_human_readable():
    sla_manifest = _load_sla_manifest_module()
    manifest = sla_manifest.load_manifest()
    inventory = sla_manifest.format_case_inventory(manifest)

    assert "required:" in inventory
    assert "managed_codex_warm_live_graceful_close provider=codex" in inventory
    assert "experimental:" in inventory
    assert "undefined:" in inventory
