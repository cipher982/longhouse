from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest

from zerg import provider_live_proof as plp
from zerg import provider_release_status as prs


@pytest.fixture(autouse=True)
def clear_live_proof_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(prs.PROVIDER_LIVE_PROOF_DIR_ENV, raising=False)
    monkeypatch.setenv("LONGHOUSE_HOME", str(tmp_path / ".longhouse"))
    monkeypatch.setenv(prs.PROVIDER_RELEASE_STATUS_CONFIG_FILE_ENV, str(tmp_path / "missing-provider-status.env"))


def _generated_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _artifact(provider: str, version: str) -> dict[str, object]:
    return {
        "schema_version": prs.PROVIDER_STATUS_SCHEMA_VERSION,
        "artifact_kind": plp.LIVE_PROOF_ARTIFACT_KIND,
        "provider": provider,
        "provider_version": version,
        "generated_at": _generated_at(),
        "verdict": "green",
        "operation_evidence": {
            "send_input": {
                "status": "pass",
                "level": "live_no_token",
                "source": "provider-live-canary",
                "canary": "send_input_contract",
            }
        },
        "evidence_root": "/tmp/evidence",
    }


def test_matching_live_proof_applies(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "claude.json").write_text(json.dumps(_artifact("claude", "2.1.153")), encoding="utf-8")
    monkeypatch.setenv(prs.PROVIDER_LIVE_PROOF_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(
        plp,
        "_provider_version_from_cli",
        lambda path: ("Claude Code 2.1.153\n", None),
    )

    proof = plp.collect_provider_live_proof({"claude": {"path": "/opt/homebrew/bin/claude"}})

    claude = proof["statuses"]["claude"]
    assert proof["enabled"] is True
    assert claude["status"] == "ok"
    assert claude["applies"] is True
    assert claude["version_match"] == "match"
    assert claude["operation_evidence"]["send_input"]["level"] == "live_no_token"


def test_mismatched_live_proof_is_informational_only(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "opencode.json").write_text(json.dumps(_artifact("opencode", "1.15.11")), encoding="utf-8")
    monkeypatch.setenv(prs.PROVIDER_LIVE_PROOF_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(
        plp,
        "_provider_version_from_cli",
        lambda path: ("1.15.12\n", None),
    )

    proof = plp.collect_provider_live_proof({"opencode": {"path": "/opt/homebrew/bin/opencode"}})

    opencode = proof["statuses"]["opencode"]
    assert opencode["status"] == "version_mismatch"
    assert opencode["applies"] is False
    assert opencode["version_match"] == "mismatch"
    assert opencode["operation_evidence"]["send_input"]["status"] == "pass"


def test_rejects_release_artifact_in_live_proof_dir(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "antigravity.json").write_text(
        json.dumps(
            {
                "schema_version": prs.PROVIDER_STATUS_SCHEMA_VERSION,
                "provider": "antigravity",
                "provider_version": "1.0.2",
                "generated_at": _generated_at(),
                "verdict": "green",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(prs.PROVIDER_LIVE_PROOF_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(
        plp,
        "_provider_version_from_cli",
        lambda path: ("1.0.2\n", None),
    )

    proof = plp.collect_provider_live_proof({"antigravity": {"path": "/Users/test/.local/bin/agy"}})

    antigravity = proof["statuses"]["antigravity"]
    assert antigravity["status"] == "artifact_kind_mismatch"
    assert antigravity["applies"] is False
    assert antigravity["artifact_kind_status"] == "mismatch"


def test_stale_live_proof_does_not_apply(monkeypatch, tmp_path: Path) -> None:
    artifact = _artifact("claude", "2.1.153")
    artifact["generated_at"] = "2000-01-01T00:00:00Z"
    (tmp_path / "claude.json").write_text(json.dumps(artifact), encoding="utf-8")
    monkeypatch.setenv(prs.PROVIDER_LIVE_PROOF_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(plp, "_max_artifact_age_seconds", lambda: 1)
    monkeypatch.setattr(
        plp,
        "_provider_version_from_cli",
        lambda path: ("Claude Code 2.1.153\n", None),
    )

    proof = plp.collect_provider_live_proof({"claude": {"path": "/opt/homebrew/bin/claude"}})

    claude = proof["statuses"]["claude"]
    assert claude["status"] == "stale"
    assert claude["applies"] is False
    assert claude["version_match"] == "match"


def test_schema_mismatch_live_proof_does_not_apply(monkeypatch, tmp_path: Path) -> None:
    artifact = _artifact("claude", "2.1.153")
    artifact["schema_version"] = 99
    (tmp_path / "claude.json").write_text(json.dumps(artifact), encoding="utf-8")
    monkeypatch.setenv(prs.PROVIDER_LIVE_PROOF_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(
        plp,
        "_provider_version_from_cli",
        lambda path: ("Claude Code 2.1.153\n", None),
    )

    proof = plp.collect_provider_live_proof({"claude": {"path": "/opt/homebrew/bin/claude"}})

    claude = proof["statuses"]["claude"]
    assert claude["status"] == "schema_mismatch"
    assert claude["applies"] is False
    assert claude["schema_status"] == "mismatch"


def test_provider_mismatch_live_proof_does_not_apply(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "claude.json").write_text(json.dumps(_artifact("opencode", "2.1.153")), encoding="utf-8")
    monkeypatch.setenv(prs.PROVIDER_LIVE_PROOF_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(
        plp,
        "_provider_version_from_cli",
        lambda path: ("Claude Code 2.1.153\n", None),
    )

    proof = plp.collect_provider_live_proof({"claude": {"path": "/opt/homebrew/bin/claude"}})

    claude = proof["statuses"]["claude"]
    assert claude["status"] == "provider_mismatch"
    assert claude["applies"] is False
    assert claude["artifact_provider_status"] == "mismatch"


def test_unknown_local_version_live_proof_does_not_apply(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "claude.json").write_text(json.dumps(_artifact("claude", "2.1.153")), encoding="utf-8")
    monkeypatch.setenv(prs.PROVIDER_LIVE_PROOF_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(
        plp,
        "_provider_version_from_cli",
        lambda path: (None, "provider CLI path missing"),
    )

    proof = plp.collect_provider_live_proof({"claude": {"path": None}})

    claude = proof["statuses"]["claude"]
    assert claude["status"] == "unknown_local_version"
    assert claude["applies"] is False
    assert claude["version_match"] == "unknown_local"


def test_not_configured_when_live_proof_dir_absent(monkeypatch) -> None:
    monkeypatch.setattr(
        plp,
        "_provider_version_from_cli",
        lambda path: ("2.1.153\n", None),
    )

    proof = plp.collect_provider_live_proof({"claude": {"path": "/opt/homebrew/bin/claude"}})

    assert proof["enabled"] is False
    assert proof["statuses"]["claude"]["status"] == "not_configured"


def test_default_live_proof_dir_applies_from_longhouse_home(monkeypatch, tmp_path: Path) -> None:
    longhouse_home = tmp_path / ".longhouse-dev"
    proof_dir = longhouse_home / "provider-live-proof"
    proof_dir.mkdir(parents=True)
    (proof_dir / "claude.json").write_text(json.dumps(_artifact("claude", "2.1.153")), encoding="utf-8")
    monkeypatch.setenv("LONGHOUSE_HOME", str(longhouse_home))
    monkeypatch.setattr(
        plp,
        "_provider_version_from_cli",
        lambda path: ("Claude Code 2.1.153\n", None),
    )

    proof = plp.collect_provider_live_proof({"claude": {"path": "/opt/homebrew/bin/claude"}})

    claude = proof["statuses"]["claude"]
    assert proof["enabled"] is True
    assert claude["status"] == "ok"
    assert claude["applies"] is True
    assert claude["source"]["attempts"][0]["path"] == str(proof_dir / "claude.json")


def test_missing_default_live_proof_dir_stays_not_configured(monkeypatch, tmp_path: Path) -> None:
    longhouse_home = tmp_path / ".longhouse-dev"
    monkeypatch.setenv("LONGHOUSE_HOME", str(longhouse_home))
    monkeypatch.setattr(
        plp,
        "_provider_version_from_cli",
        lambda path: ("Claude Code 2.1.153\n", None),
    )

    proof = plp.collect_provider_live_proof({"claude": {"path": "/opt/homebrew/bin/claude"}})

    claude = proof["statuses"]["claude"]
    assert proof["enabled"] is False
    assert claude["configured"] is False
    assert claude["status"] == "not_configured"
    assert claude["source"]["attempts"][0]["path"] == str(longhouse_home / "provider-live-proof" / "claude.json")


def test_corrupt_default_live_proof_file_is_unavailable(monkeypatch, tmp_path: Path) -> None:
    longhouse_home = tmp_path / ".longhouse-dev"
    proof_dir = longhouse_home / "provider-live-proof"
    proof_dir.mkdir(parents=True)
    (proof_dir / "claude.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("LONGHOUSE_HOME", str(longhouse_home))

    proof = plp.collect_provider_live_proof({"claude": {"path": "/opt/homebrew/bin/claude"}})

    claude = proof["statuses"]["claude"]
    assert proof["enabled"] is True
    assert claude["configured"] is True
    assert claude["status"] == "unavailable"
    assert "JSONDecodeError" in claude["source"]["attempts"][0]["error"]


def test_configured_live_proof_dir_overrides_default(monkeypatch, tmp_path: Path) -> None:
    longhouse_home = tmp_path / ".longhouse-dev"
    default_dir = longhouse_home / "provider-live-proof"
    explicit_dir = tmp_path / "explicit-proof"
    default_dir.mkdir(parents=True)
    explicit_dir.mkdir()
    (default_dir / "claude.json").write_text(json.dumps(_artifact("claude", "2.1.153")), encoding="utf-8")
    (explicit_dir / "claude.json").write_text(json.dumps(_artifact("claude", "9.9.9")), encoding="utf-8")
    monkeypatch.setenv("LONGHOUSE_HOME", str(longhouse_home))
    monkeypatch.setenv(prs.PROVIDER_LIVE_PROOF_DIR_ENV, str(explicit_dir))
    monkeypatch.setattr(
        plp,
        "_provider_version_from_cli",
        lambda path: ("Claude Code 2.1.153\n", None),
    )

    proof = plp.collect_provider_live_proof({"claude": {"path": "/opt/homebrew/bin/claude"}})

    claude = proof["statuses"]["claude"]
    assert claude["status"] == "version_mismatch"
    assert claude["source"]["attempts"][0]["path"] == str(explicit_dir / "claude.json")


def test_config_file_live_proof_dir_overrides_default(monkeypatch, tmp_path: Path) -> None:
    longhouse_home = tmp_path / ".longhouse-dev"
    default_dir = longhouse_home / "provider-live-proof"
    configured_dir = tmp_path / "configured-proof"
    config_path = tmp_path / "provider-release-status.env"
    default_dir.mkdir(parents=True)
    configured_dir.mkdir()
    (default_dir / "claude.json").write_text(json.dumps(_artifact("claude", "2.1.153")), encoding="utf-8")
    (configured_dir / "claude.json").write_text(json.dumps(_artifact("claude", "9.9.9")), encoding="utf-8")
    config_path.write_text(f"{prs.PROVIDER_LIVE_PROOF_DIR_ENV}={configured_dir}\n", encoding="utf-8")
    monkeypatch.delenv(prs.PROVIDER_LIVE_PROOF_DIR_ENV, raising=False)
    monkeypatch.setenv("LONGHOUSE_HOME", str(longhouse_home))
    monkeypatch.setenv(prs.PROVIDER_RELEASE_STATUS_CONFIG_FILE_ENV, str(config_path))
    monkeypatch.setattr(
        plp,
        "_provider_version_from_cli",
        lambda path: ("Claude Code 2.1.153\n", None),
    )

    proof = plp.collect_provider_live_proof({"claude": {"path": "/opt/homebrew/bin/claude"}})

    claude = proof["statuses"]["claude"]
    assert claude["status"] == "version_mismatch"
    assert claude["source"]["attempts"][0]["path"] == str(configured_dir / "claude.json")
