from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).with_name("cargo.py")
SPEC = importlib.util.spec_from_file_location("longhouse_cargo", SCRIPT)
assert SPEC and SPEC.loader
cargo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cargo)


def configure_target(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    target = root / "cargo-target"
    monkeypatch.setenv("LONGHOUSE_CARGO_TARGET_DIR", str(target))
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    monkeypatch.setattr(cargo, "LOCK_DIR", root / "locks")
    return target


def test_target_precedence_prefers_longhouse_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = configure_target(monkeypatch, tmp_path)
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "cargo-env-target"))
    assert cargo.target_dir() == target


def test_artifact_paths_use_profile_layout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = configure_target(monkeypatch, tmp_path)
    assert cargo.artifact_path("dev", "longhouse") == target / "debug" / "longhouse"
    assert cargo.artifact_path("test", "longhouse-engine") == target / "debug" / "longhouse-engine"
    assert cargo.artifact_path("ci", "longhouse-engine") == target / "ci" / "longhouse-engine"
    assert cargo.artifact_path("release", "longhouse", "aarch64-unknown-linux-gnu") == (
        target / "aarch64-unknown-linux-gnu" / "release" / "longhouse"
    )


def test_run_cargo_marks_target_and_reuses_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = configure_target(monkeypatch, tmp_path)
    assert cargo.run_cargo(["--version"]) == 0
    marker = target / cargo.MARKER_NAME
    assert marker.exists()
    assert cargo.run_cargo(["--version"]) == 0


def test_preflight_rejects_over_budget_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = configure_target(monkeypatch, tmp_path)
    target.mkdir()
    (target / "large").write_bytes(b"x" * 4096)
    monkeypatch.setenv("LONGHOUSE_CARGO_TARGET_BUDGET_GB", "0.000001")
    assert cargo.health(fail_over_budget=True) == 2


def test_clean_requires_owned_marker_and_removes_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = configure_target(monkeypatch, tmp_path)
    cargo._ensure_marker(target)
    (target / "debug").mkdir()
    (target / "debug" / "artifact").write_text("generated", encoding="utf-8")
    assert cargo.clean() == 0
    assert not target.exists()


def test_clean_rejects_symlinked_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real_target = tmp_path / "real-target"
    real_target.mkdir()
    link = tmp_path / "cargo-target"
    link.symlink_to(real_target, target_is_directory=True)
    monkeypatch.setenv("LONGHOUSE_CARGO_TARGET_DIR", str(link))
    with pytest.raises(SystemExit, match="symlinked"):
        cargo.clean()


def test_an_exact_checkout_borrows_the_owners_target_and_its_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    owner = tmp_path / "primary"
    monkeypatch.delenv("LONGHOUSE_CARGO_TARGET_DIR", raising=False)
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    monkeypatch.setenv(cargo.OWNER_ENV, str(owner))
    target = cargo.target_dir()
    assert target == owner / ".build" / "cargo-target"
    assert cargo._lock_path(target).parent == owner / ".build" / "locks"
    cargo._ensure_marker(target)  # the marker names the owner, so the owner's own runs keep accepting it
    monkeypatch.setattr(cargo, "REPO_ROOT", owner)
    monkeypatch.delenv(cargo.OWNER_ENV)
    monkeypatch.setattr(cargo, "DEFAULT_TARGET_DIR", target)
    cargo._ensure_marker(target)


def test_without_an_owner_another_checkouts_target_is_still_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = configure_target(monkeypatch, tmp_path)
    monkeypatch.setenv(cargo.OWNER_ENV, str(tmp_path / "primary"))
    cargo._ensure_marker(target)
    monkeypatch.delenv(cargo.OWNER_ENV)
    with pytest.raises(SystemExit, match="another checkout"):
        cargo._ensure_marker(target)
