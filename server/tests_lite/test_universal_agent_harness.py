from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

from zerg.qa import universal_agent_harness as uah

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_exe(path: Path, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""#!/usr/bin/env python3
import sys

if sys.argv[1:] == ["--version"]:
    print({version!r})
    raise SystemExit(0)

print("unexpected args", sys.argv[1:], file=sys.stderr)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_bins(tmp_path: Path) -> dict[str, Path]:
    return {
        "claude": _write_exe(tmp_path / "bin" / "claude", "2.9.9-fake (Claude Code)"),
        "codex": _write_exe(tmp_path / "bin" / "codex", "codex-cli 9.9.9"),
        "opencode": _write_exe(tmp_path / "bin" / "opencode", "opencode 9.9.9"),
        "antigravity": _write_exe(tmp_path / "bin" / "agy", "agy 9.9.9"),
    }


def test_adapter_registry_loads_all_four_provider_mvp_adapters(tmp_path: Path) -> None:
    registry = uah.adapter_registry(_fake_bins(tmp_path))

    assert tuple(registry) == uah.SUPPORTED_PROVIDERS
    for provider, adapter in registry.items():
        assert adapter.config.provider == provider
        assert set(uah.MVP_METHODS).issubset(set(adapter.config.methods))
        assert set(uah.MVP_CAPABILITIES).issubset(set(adapter.config.capabilities))


def test_probe_identity_runs_for_all_providers_through_shared_scenario(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("probe_identity",),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
        )
    )

    assert payload["verdict"] == "green"
    assert {result["provider"] for result in payload["results"]} == set(uah.SUPPORTED_PROVIDERS)
    assert all(result["scenario"] == "probe_identity" for result in payload["results"])
    assert all(result["status"] == "pass" for result in payload["results"])
    for result in payload["results"]:
        probe = json.loads((Path(result["evidence_root"]) / "assertions" / "probe.json").read_text(encoding="utf-8"))
        assert probe["declared_capabilities"]
        assert probe["mvp_methods"] == list(uah.MVP_METHODS)
        assert probe["version"]


def test_codex_run_prompt_once_writes_safe_projection(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("run_prompt_once",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": _fake_bins(tmp_path)["codex"]},
            prompt="hello",
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    evidence_root = Path(result["evidence_root"])
    assert (evidence_root / "input" / "prompt.txt").read_text(encoding="utf-8") == "hello"
    assert (evidence_root / "assertions" / "run_prompt.json").is_file()
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["has_user"] is True
    assert session["has_assistant"] is True
    assert session["operation_statuses"]["run_once"]["status"] == "pass"


def test_unsafe_run_prompt_once_is_typed_unsupported_gap(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude",),
            scenarios=("run_prompt_once",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"claude": _fake_bins(tmp_path)["claude"]},
            prompt="hello",
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "yellow"
    assert result["status"] == "unsupported_gap"
    assert result["failure_code"] == "run_prompt_once_not_safe_no_token"
    evidence_root = Path(result["evidence_root"])
    assert (evidence_root / "input" / "prompt.txt").read_text(encoding="utf-8") == "hello"
    assert (evidence_root / "assertions" / "run_prompt.json").is_file()


def test_managed_session_scenarios_pass_for_codex_and_opencode(tmp_path: Path) -> None:
    bins = _fake_bins(tmp_path)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex", "opencode"),
            scenarios=("launch_managed_session", "send_receive"),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": bins["codex"], "opencode": bins["opencode"]},
            prompt="ping",
        )
    )

    assert payload["verdict"] == "green"
    assert len(payload["results"]) == 4
    assert all(result["status"] == "pass" for result in payload["results"])
    for result in payload["results"]:
        evidence_root = Path(result["evidence_root"])
        session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
        assert session["provider"] == result["provider"]
        assert session["provider_session_id"].startswith(f"universal-{result['provider']}-")
        if result["scenario"] == "send_receive":
            assert session["has_user"] is True
            assert session["has_assistant"] is True
            assert session["operation_statuses"]["send_input"]["status"] == "pass"
        else:
            assert session["operation_statuses"]["launch_local"]["level"] == "live_no_token"


def test_managed_session_scenarios_are_typed_gaps_for_other_providers(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude", "antigravity"),
            scenarios=("launch_managed_session", "send_receive"),
            evidence_root=tmp_path / "evidence",
            provider_bins={
                "claude": _fake_bins(tmp_path)["claude"],
                "antigravity": _fake_bins(tmp_path)["antigravity"],
            },
            prompt="ping",
        )
    )

    assert payload["verdict"] == "yellow"
    assert len(payload["results"]) == 4
    assert {result["status"] for result in payload["results"]} == {"unsupported_gap"}
    assert {result["failure_code"] for result in payload["results"]} == {
        "managed_session_not_safe_no_token",
        "send_receive_not_safe_no_token",
    }


def test_collect_raw_evidence_runs_for_all_providers_without_launching(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("collect_raw_evidence",),
            evidence_root=tmp_path / "evidence",
        )
    )

    assert payload["verdict"] == "green"
    assert len(payload["results"]) == len(uah.SUPPORTED_PROVIDERS)
    for result in payload["results"]:
        assert result["status"] == "pass"
        evidence_root = Path(result["evidence_root"])
        assert (evidence_root / "manifest.json").is_file()
        assert (evidence_root / "assertions" / "collect_raw_evidence.json").is_file()


def test_probe_failure_writes_raw_and_assertion_evidence(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "codex"
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("probe_identity",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": missing},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "red"
    assert result["status"] == "fail"
    assert result["failure_code"] == "provider_binary_not_found"
    evidence_root = Path(result["evidence_root"])
    assert (evidence_root / "manifest.json").is_file()
    assert (evidence_root / "raw" / "version-command.json").is_file()
    assert (evidence_root / "assertions" / "probe.json").is_file()


def test_parse_ingest_project_replays_fixture_without_launching_provider(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.jsonl"
    fixture.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "text": "hello"}),
                json.dumps({"type": "assistant", "text": "world"}),
                json.dumps({"type": "unknown", "payload": {"new": True}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode",),
            scenarios=("parse_ingest_project",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"opencode": tmp_path / "not-used"},
            fixture_path=fixture,
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    evidence_root = Path(result["evidence_root"])
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    timeline = json.loads((evidence_root / "longhouse" / "timeline-projection.json").read_text(encoding="utf-8"))
    unknown = (evidence_root / "events" / "unknown-provider-events.jsonl").read_text(encoding="utf-8")
    assert session["has_user"] is True
    assert session["has_assistant"] is True
    assert timeline["event_count"] == 3
    assert '"type": "unknown"' in unknown


def test_scenario_runner_does_not_branch_on_provider_names() -> None:
    sources = "\n".join(
        inspect.getsource(item)
        for item in (
            uah.run_scenario,
            uah.run_probe_identity,
            uah.run_collect_raw_evidence,
            uah.run_parse_ingest_project,
            uah.run_prompt_once,
            uah.run_launch_managed_session,
            uah.run_send_receive,
        )
    )

    for provider in uah.SUPPORTED_PROVIDERS:
        assert provider not in sources


def test_script_entrypoint_emits_normalized_artifact(tmp_path: Path) -> None:
    fake_bin = _fake_bins(tmp_path)["claude"]
    artifact_root = tmp_path / "cli-evidence"

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "qa" / "universal-agent-harness.py"),
            "--provider",
            "claude",
            "--scenario",
            "probe_identity",
            "--provider-bin",
            str(fake_bin),
            "--evidence-root",
            str(artifact_root),
            "--json",
        ],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["artifact_kind"] == uah.ARTIFACT_KIND
    assert payload["verdict"] == "green"
    assert (artifact_root / "universal-agent-harness.json").is_file()
