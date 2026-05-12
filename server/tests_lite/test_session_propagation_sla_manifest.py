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
    summary = sla_manifest.manifest_summary(manifest)
    assert summary["schema_version"] == 1
    assert summary["cases"]["required"] == 1
    assert summary["cases"]["experimental"] >= 9
    assert summary["cases"]["undefined"] >= 3
    assert summary["ci_modes"]["gate"] == 1
    assert summary["ci_modes"]["report"] >= 3
    assert summary["ci_modes"]["blocked"] >= 6
    assert summary["metrics"] >= 10


def test_session_propagation_sla_manifest_keeps_codex_required_path():
    sla_manifest = _load_sla_manifest_module()
    manifest = sla_manifest.load_manifest()
    required = sla_manifest.cases_by_status(manifest, "required")

    assert [case["id"] for case in required] == ["managed_codex_warm_live_graceful_close"]
    case = required[0]
    assert case["provider"] == "codex"
    assert case["control_path"] == "managed"
    assert case["topology"] == "hosted_runtime_host"
    assert case["profile"] == "warm-live"
    assert case["profiler_driver"] == "managed_codex_warm_live"
    assert case["ci_mode"] == "gate"
    assert "timeline_sse" in case["required_observers"]
    assert "browser_card" in case["required_observers"]
    assert "warm_close_local_to_sse_ms" not in case["metrics"]
    assert "warm_close_sse_to_paint_ms" not in case["metrics"]
    assert "durable_archive_local_to_hosted_ms" not in case["metrics"]

    durable_case = sla_manifest.case_by_id(manifest, "managed_codex_durable_archive")
    assert durable_case is not None
    assert durable_case["status"] == "experimental"
    assert durable_case["profile_class"] == "durable_archive"
    assert durable_case["metrics"] == ["durable_archive_local_to_hosted_ms"]

    cold_case = sla_manifest.case_by_id(manifest, "managed_codex_cold_timeline_closed")
    assert cold_case is not None
    assert cold_case["status"] == "experimental"
    assert cold_case["profile"] == "cold-timeline"
    assert cold_case["profile_class"] == "cold_timeline"
    assert cold_case["ci_mode"] == "report"
    assert cold_case["metrics"] == [
        "cold_timeline_navigation_to_card_paint_ms",
        "cold_timeline_navigation_to_close_paint_ms",
    ]


def test_session_propagation_sla_tracks_provider_coverage_and_ci_readiness():
    sla_manifest = _load_sla_manifest_module()
    manifest = sla_manifest.load_manifest()

    cases = {case["id"]: case for case in manifest["cases"]}
    assert cases["managed_claude_warm_live_graceful_close"]["ci_mode"] == "blocked"
    assert "blocked_reason" in cases["managed_claude_warm_live_graceful_close"]
    assert cases["unmanaged_codex_direct_graceful_close"]["ci_mode"] == "report"
    assert cases["unmanaged_codex_direct_graceful_close"]["profiler_driver"] == "unmanaged_codex_baseline"
    assert cases["managed_opencode_warm_lifecycle"]["ci_mode"] == "blocked"

    explicit_combos = {
        (case["provider"], case["control_path"])
        for case in manifest["cases"]
        if case["status"] != "undefined" and case["provider"] != "all"
    }
    assert ("codex", "managed") in explicit_combos
    assert ("codex", "unmanaged") in explicit_combos
    assert ("claude", "managed") in explicit_combos
    assert ("claude", "unmanaged") in explicit_combos
    assert ("opencode", "managed") in explicit_combos


def test_session_propagation_sla_metric_aliases_support_existing_profiler_names():
    sla_manifest = _load_sla_manifest_module()
    manifest = sla_manifest.load_manifest()

    assert sla_manifest.metric_target_ms(manifest, "live_first_from_local_ms") == 500
    assert sla_manifest.metric_target_ms(manifest, "close_observed_ms") == 1000
    assert sla_manifest.metric_target_ms(manifest, "durable_archive_local_to_hosted_ms") == 3000
    assert sla_manifest.metric_target_ms(manifest, "cold_timeline_navigation_to_card_paint_ms") == 2000


def test_session_propagation_sla_marks_close_watcher_metrics_diagnostic():
    sla_manifest = _load_sla_manifest_module()
    manifest = sla_manifest.load_manifest()

    assert sla_manifest.metric_is_diagnostic(manifest, "warm_close_local_to_sse_ms") is True
    assert sla_manifest.metric_is_diagnostic(manifest, "warm_close_sse_to_paint_ms") is True
    assert sla_manifest.metric_is_diagnostic(manifest, "warm_close_local_to_paint_ms") is False


def test_session_propagation_sla_undefined_cases_do_not_declare_observers_or_metrics():
    sla_manifest = _load_sla_manifest_module()
    manifest = sla_manifest.load_manifest()

    for case in sla_manifest.cases_by_status(manifest, "undefined"):
        assert case["truth_source"] == "none"
        assert case["required_observers"] == []
        assert case["metrics"] == []


def test_session_propagation_sla_inventory_is_human_readable():
    sla_manifest = _load_sla_manifest_module()
    manifest = sla_manifest.load_manifest()
    inventory = sla_manifest.format_case_inventory(manifest)

    assert "required:" in inventory
    assert "managed_codex_warm_live_graceful_close provider=codex" in inventory
    assert "experimental:" in inventory
    assert "undefined:" in inventory
