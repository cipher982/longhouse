from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "generate" / "validate_host_link.py"
SCHEMA = ROOT / "schemas" / "host_link.yml"


def _check(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--check", "--schema", str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_host_link_contract_is_valid() -> None:
    result = _check(SCHEMA)
    assert result.returncode == 0, result.stderr


def test_host_link_contract_checker_rejects_bad_state_edit(tmp_path: Path) -> None:
    schema = yaml.safe_load(SCHEMA.read_text(encoding="utf-8"))
    schema["states"]["host_link"].remove("unreachable")
    invalid_schema = tmp_path / "host_link.yml"
    invalid_schema.write_text(yaml.safe_dump(schema, sort_keys=False), encoding="utf-8")

    result = _check(invalid_schema)

    assert result.returncode == 1
    assert "schema.states.host_link" in result.stderr


def test_host_link_contract_checker_rejects_unexpected_keys(tmp_path: Path) -> None:
    schema = yaml.safe_load(SCHEMA.read_text(encoding="utf-8"))
    schema["k1"]["future_field"] = "not allowed"
    invalid_schema = tmp_path / "host_link.yml"
    invalid_schema.write_text(yaml.safe_dump(schema, sort_keys=False), encoding="utf-8")

    result = _check(invalid_schema)

    assert result.returncode == 1
    assert "schema.k1 has unexpected keys: future_field" in result.stderr


def test_host_link_contract_checker_requires_exact_scalar_types(tmp_path: Path) -> None:
    for name, value in (("boolean", True), ("float", 1.0)):
        schema = yaml.safe_load(SCHEMA.read_text(encoding="utf-8"))
        schema["k1"]["retry_after_min_seconds"] = value
        invalid_schema = tmp_path / f"host_link_{name}.yml"
        invalid_schema.write_text(yaml.safe_dump(schema, sort_keys=False), encoding="utf-8")

        result = _check(invalid_schema)

        assert result.returncode == 1
        assert "schema.k1.retry_after_min_seconds" in result.stderr
