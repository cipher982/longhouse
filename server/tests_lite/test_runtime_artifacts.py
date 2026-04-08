from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.services import runtime_artifacts


def test_ensure_runtime_binary_uses_existing_engine_on_path(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    existing = tmp_path / "existing" / "longhouse-engine"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("engine")
    existing.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_artifacts, "_local_bin_dir", lambda: home / ".local" / "bin")
    monkeypatch.setattr(runtime_artifacts.shutil, "which", lambda name: str(existing) if name == "longhouse-engine" else None)

    result = runtime_artifacts.ensure_runtime_binary(runtime_artifacts.RuntimeComponent.ENGINE)

    assert result.path == str(existing)
    assert result.launch_path == str(existing)
    assert result.installed_now is False
    assert result.source == "path"


def test_ensure_runtime_binary_copies_from_local_override(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "build" / "longhouse-local-health-menubar"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("menubar")
    source.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_artifacts, "_local_bin_dir", lambda: home / ".local" / "bin")

    result = runtime_artifacts.ensure_runtime_binary(
        runtime_artifacts.RuntimeComponent.LOCAL_HEALTH_MENUBAR,
        source_override=str(source),
    )

    destination = home / ".local" / "bin" / "longhouse-local-health-menubar"
    assert destination.exists()
    assert destination.read_text() == "menubar"
    assert result.path == str(destination)
    assert result.launch_path == str(destination)
    assert result.installed_now is True


def test_ensure_runtime_artifact_copies_app_bundle_from_local_override(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    source_app = tmp_path / "build" / "Longhouse.app"
    executable = source_app / "Contents" / "MacOS" / "Longhouse"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("app")
    executable.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_artifacts, "_local_applications_dir", lambda: home / "Applications")

    result = runtime_artifacts.ensure_runtime_artifact(
        runtime_artifacts.RuntimeComponent.LOCAL_HEALTH_APP,
        source_override=str(source_app),
    )

    destination = home / "Applications" / "Longhouse.app"
    assert destination.exists()
    assert (destination / "Contents" / "MacOS" / "Longhouse").read_text() == "app"
    assert result.path == str(destination)
    assert result.launch_path == str(destination / "Contents" / "MacOS" / "Longhouse")
    assert result.installed_now is True
    assert result.kind == runtime_artifacts.RuntimeArtifactKind.APP_BUNDLE


def test_ensure_runtime_binary_uses_release_url_when_override_missing(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()

    downloads: list[tuple[runtime_artifacts.RuntimeComponent, str, Path]] = []

    def fake_install_remote(component: runtime_artifacts.RuntimeComponent, url: str, destination_path: Path) -> None:
        downloads.append((component, url, destination_path))
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_text("binary")
        destination_path.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_artifacts, "_local_bin_dir", lambda: home / ".local" / "bin")
    monkeypatch.setattr(runtime_artifacts, "_install_artifact_from_remote_source", fake_install_remote)
    monkeypatch.setattr(runtime_artifacts, "_platform_target", lambda: "linux-x64")
    monkeypatch.setattr(runtime_artifacts.metadata, "version", lambda package: "0.1.9")
    monkeypatch.setattr(runtime_artifacts.shutil, "which", lambda name: None)

    result = runtime_artifacts.ensure_runtime_binary(runtime_artifacts.RuntimeComponent.ENGINE)

    assert result.path == str(home / ".local" / "bin" / "longhouse-engine")
    assert result.launch_path == str(home / ".local" / "bin" / "longhouse-engine")
    assert result.installed_now is True
    assert downloads == [
        (
            runtime_artifacts.RuntimeComponent.ENGINE,
            "https://github.com/cipher982/longhouse/releases/download/v0.1.9/longhouse-engine-linux-x64",
            home / ".local" / "bin" / "longhouse-engine",
        )
    ]


def test_ensure_runtime_artifact_uses_release_url_for_app_bundle(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()

    downloads: list[tuple[runtime_artifacts.RuntimeComponent, str, Path]] = []

    def fake_install_remote(component: runtime_artifacts.RuntimeComponent, url: str, destination_path: Path) -> None:
        downloads.append((component, url, destination_path))
        executable = destination_path / "Contents" / "MacOS" / "Longhouse"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("app")
        executable.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_artifacts, "_local_applications_dir", lambda: home / "Applications")
    monkeypatch.setattr(runtime_artifacts, "_install_artifact_from_remote_source", fake_install_remote)
    monkeypatch.setattr(runtime_artifacts, "_platform_target", lambda: "darwin-arm64")
    monkeypatch.setattr(runtime_artifacts.metadata, "version", lambda package: "0.1.9")

    result = runtime_artifacts.ensure_runtime_artifact(runtime_artifacts.RuntimeComponent.LOCAL_HEALTH_APP)

    expected_path = home / "Applications" / "Longhouse.app"
    assert result.path == str(expected_path)
    assert result.launch_path == str(expected_path / "Contents" / "MacOS" / "Longhouse")
    assert result.installed_now is True
    assert result.kind == runtime_artifacts.RuntimeArtifactKind.APP_BUNDLE
    assert downloads == [
        (
            runtime_artifacts.RuntimeComponent.LOCAL_HEALTH_APP,
            "https://github.com/cipher982/longhouse/releases/download/v0.1.9/longhouse-local-health-app-darwin-arm64.zip",
            expected_path,
        )
    ]
