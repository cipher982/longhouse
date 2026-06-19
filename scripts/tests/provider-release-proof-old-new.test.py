#!/usr/bin/env python3
"""Smoke tests for staged old/new provider release-proof runner."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _write_fake_provider_bin(path: Path, version: str) -> Path:
    return _write_exe(
        path,
        f"""#!/usr/bin/env python3
import sys

if sys.argv[1:] == ["--version"]:
    print({version!r})
    raise SystemExit(0)

print("unexpected args", sys.argv[1:], file=sys.stderr)
raise SystemExit(2)
""",
    )


def _write_fake_repo(root: Path) -> None:
    _write_exe(
        root / "scripts" / "qa" / "provider-release-proof.py",
        r"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

args = sys.argv[1:]

def value(flag, default=None):
    return args[args.index(flag) + 1] if flag in args else default

provider_bin = Path(value("--provider-bin"))
if not provider_bin.is_file():
    raise SystemExit(2)

provider = value("--provider")
artifact = Path(value("--artifact"))
provider_version = value("--provider-version", provider_bin.read_text(encoding="utf-8").strip())
payload = {
    "artifact_kind": "provider_release_proof",
    "provider": provider,
    "provider_version": provider_version,
    "scenario_id": value("--scenario-id"),
    "verdict": "green",
    "failure_code": None,
    "canaries": {"fake_source": {"status": "pass"}},
    "operation_evidence": {"launch_local": {"status": "pass", "level": "fixture"}},
}
artifact.parent.mkdir(parents=True, exist_ok=True)
artifact.write_text(json.dumps(payload), encoding="utf-8")
print(json.dumps(payload))
raise SystemExit(0)
""",
    )
    _write_exe(
        root / "scripts" / "qa" / "provider-release-proof-baseline.py",
        r"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

args = sys.argv[1:]

def value(flag):
    return args[args.index(flag) + 1]

old = json.loads(Path(value("--old")).read_text(encoding="utf-8"))
new = json.loads(Path(value("--new")).read_text(encoding="utf-8"))
verdict = "green" if old.get("provider") == new.get("provider") else "red"
payload = {
    "artifact_kind": "provider_release_proof_old_new_diff",
    "provider": new.get("provider"),
    "verdict": verdict,
    "failure_code": None if verdict == "green" else "provider_mismatch",
    "staging": {"status": "explicit_proof_artifacts"},
}
artifact = Path(value("--artifact"))
artifact.parent.mkdir(parents=True, exist_ok=True)
artifact.write_text(json.dumps(payload), encoding="utf-8")
print(json.dumps(payload))
raise SystemExit(0 if verdict == "green" else 1)
""",
    )


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "qa" / "provider-release-proof-old-new.py"), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_staged_old_new_runner_produces_old_new_proofs_and_diff() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_repo = root / "repo"
        _write_fake_repo(fake_repo)
        old_bin = _write_fake_provider_bin(root / "bin" / "opencode-old", "opencode 1.2.3")
        new_bin = _write_fake_provider_bin(root / "bin" / "opencode-new", "opencode 1.2.4")
        artifact = root / "staged-old-new.json"
        evidence_root = root / "evidence"

        result = _run(
            [
                "--provider",
                "opencode",
                "--repo-root",
                str(fake_repo),
                "--old-provider-bin",
                str(old_bin),
                "--new-provider-bin",
                str(new_bin),
                "--old-provider-version",
                "opencode 1.2.3",
                "--new-provider-version",
                "opencode 1.2.4",
                "--source-review-status",
                "pass",
                "--source-review-note",
                "fixture source review passed",
                "--universal-scenario",
                "probe_identity",
                "--artifact",
                str(artifact),
                "--evidence-root",
                str(evidence_root),
                "--json",
            ]
        )

        assert result.returncode == 0, result.stderr + result.stdout
        payload = json.loads(result.stdout)
        assert payload["artifact_kind"] == "provider_release_proof_staged_old_new"
        assert payload["verdict"] == "green"
        assert payload["staging"]["status"] == "staged_provider_binaries"
        assert Path(payload["proofs"]["old"]["artifact_path"]).is_file()
        assert Path(payload["proofs"]["new"]["artifact_path"]).is_file()
        assert Path(payload["diff"]["artifact_path"]).is_file()
        assert Path(payload["artifact_path"]) == artifact
        assert _read_json(artifact)["verdict"] == "green"
        assert payload["proofs"]["old"]["provider_version"] == "opencode 1.2.3"
        assert payload["proofs"]["new"]["provider_version"] == "opencode 1.2.4"
        assert payload["diff"]["summary"]["staging"]["status"] == "explicit_proof_artifacts"


def test_staged_old_new_runner_reports_red_when_a_side_cannot_write_artifact() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_repo = root / "repo"
        _write_fake_repo(fake_repo)
        old_bin = _write_fake_provider_bin(root / "bin" / "opencode-old", "opencode 1.2.3")
        missing_new_bin = root / "bin" / "missing-opencode"

        result = _run(
            [
                "--provider",
                "opencode",
                "--repo-root",
                str(fake_repo),
                "--old-provider-bin",
                str(old_bin),
                "--new-provider-bin",
                str(missing_new_bin),
                "--source-review-status",
                "pass",
                "--universal-scenario",
                "probe_identity",
                "--evidence-root",
                str(root / "evidence"),
                "--json",
            ]
        )

        assert result.returncode == 1
        payload = json.loads(result.stdout)
        assert payload["verdict"] == "red"
        assert payload["proofs"]["new"]["failure_code"] in {
            "provider_release_proof_missing_artifact",
            "provider_release_proof_timeout",
        }
        assert payload["diff"]["failure_code"] == "old_or_new_release_proof_red"


def main() -> int:
    tests = [
        test_staged_old_new_runner_produces_old_new_proofs_and_diff,
        test_staged_old_new_runner_reports_red_when_a_side_cannot_write_artifact,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
