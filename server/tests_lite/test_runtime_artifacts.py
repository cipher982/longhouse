from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.services import runtime_artifacts


def test_desktop_app_canonical_bundle_path_is_system_applications():
    assert runtime_artifacts.desktop_app_canonical_bundle_path() == Path("/Applications/Longhouse.app")


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


def test_ensure_runtime_binary_copies_window_host_from_local_override(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "build" / "longhouse-desktop-window"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("window")
    source.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_artifacts, "_local_bin_dir", lambda: home / ".local" / "bin")

    result = runtime_artifacts.ensure_runtime_binary(
        runtime_artifacts.RuntimeComponent.DESKTOP_WINDOW,
        source_override=str(source),
    )

    destination = home / ".local" / "bin" / "longhouse-desktop-window"
    assert destination.exists()
    assert destination.read_text() == "window"
    assert result.path == str(destination)
    assert result.launch_path == str(destination)
    assert result.installed_now is True


def test_extract_version_token_accepts_build_metadata():
    assert runtime_artifacts._extract_version_token("codex-cli 0.122.0+longhouse.1") == "0.122.0+longhouse.1"


def test_ensure_runtime_binary_copies_managed_codex_from_local_override(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "build" / "codex"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("#!/bin/sh\necho codex\n")
    source.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_artifacts, "_local_bin_dir", lambda: home / ".local" / "bin")
    monkeypatch.setattr(runtime_artifacts, "resolve_longhouse_home", lambda: home / ".longhouse")
    monkeypatch.setattr(runtime_artifacts, "_probe_executable_version", lambda path: "codex-cli 0.121.0")

    result = runtime_artifacts.ensure_runtime_binary(
        runtime_artifacts.RuntimeComponent.MANAGED_CODEX,
        source_override=str(source),
    )

    launcher = home / ".local" / "bin" / "longhouse-codex"
    install_root = home / ".longhouse" / "runtimes" / "codex"
    versions = list((install_root / "versions").iterdir())
    assert len(versions) == 1
    installed_binary = versions[0] / "longhouse-codex"
    launch_alias = install_root / "current" / runtime_artifacts.MANAGED_CODEX_LAUNCH_ALIAS

    assert launcher.exists()
    assert runtime_artifacts.MANAGED_CODEX_LAUNCHER_MARKER in launcher.read_text()
    assert installed_binary.read_text() == source.read_text()
    assert (install_root / "current").resolve() == versions[0]
    assert launch_alias.is_symlink()
    assert os.readlink(launch_alias) == "longhouse-codex"
    completed = subprocess.run([str(launcher)], check=False, capture_output=True, text=True)
    assert completed.stdout.strip() == "codex"
    assert result.path == str(launcher)
    assert result.launch_path == str(launch_alias)
    assert result.installed_now is True


def test_ensure_runtime_binary_accepts_pathlike_managed_codex_override(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "build" / "codex"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("#!/bin/sh\necho codex-pathlike\n")
    source.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_artifacts, "_local_bin_dir", lambda: home / ".local" / "bin")
    monkeypatch.setattr(runtime_artifacts, "resolve_longhouse_home", lambda: home / ".longhouse")
    monkeypatch.setattr(runtime_artifacts, "_probe_executable_version", lambda path: "codex-cli 0.122.0+longhouse.1")

    result = runtime_artifacts.ensure_runtime_binary(
        runtime_artifacts.RuntimeComponent.MANAGED_CODEX,
        source_override=source,
    )

    launcher = home / ".local" / "bin" / "longhouse-codex"
    launch_alias = home / ".longhouse" / "runtimes" / "codex" / "current" / runtime_artifacts.MANAGED_CODEX_LAUNCH_ALIAS
    completed = subprocess.run([str(launcher)], check=False, capture_output=True, text=True)
    assert completed.stdout.strip() == "codex-pathlike"
    assert result.path == str(launcher)
    assert result.launch_path == str(launch_alias)
    assert result.installed_now is True


def test_ensure_runtime_binary_migrates_legacy_managed_codex_install(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    launcher = home / ".local" / "bin" / "longhouse-codex"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/bin/sh\necho legacy-codex\n")
    launcher.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_artifacts, "_local_bin_dir", lambda: home / ".local" / "bin")
    monkeypatch.setattr(runtime_artifacts, "resolve_longhouse_home", lambda: home / ".longhouse")
    monkeypatch.setattr(runtime_artifacts, "_probe_executable_version", lambda path: "codex-cli 0.120.0")

    result = runtime_artifacts.ensure_runtime_binary(runtime_artifacts.RuntimeComponent.MANAGED_CODEX)

    install_root = home / ".longhouse" / "runtimes" / "codex"
    versions = list((install_root / "versions").iterdir())
    assert len(versions) == 1
    installed_binary = versions[0] / "longhouse-codex"
    launch_alias = install_root / "current" / runtime_artifacts.MANAGED_CODEX_LAUNCH_ALIAS

    assert result.installed_now is True
    assert runtime_artifacts.MANAGED_CODEX_LAUNCHER_MARKER in launcher.read_text()
    assert installed_binary.read_text() == "#!/bin/sh\necho legacy-codex\n"
    assert launch_alias.is_symlink()
    assert os.readlink(launch_alias) == "longhouse-codex"
    completed = subprocess.run([str(launcher)], check=False, capture_output=True, text=True)
    assert completed.stdout.strip() == "legacy-codex"

    resolved = runtime_artifacts.resolve_installed_runtime_artifact(runtime_artifacts.RuntimeComponent.MANAGED_CODEX)
    assert resolved is not None
    assert resolved.source == "local-runtime-managed"
    assert resolved.path == str(launcher)
    assert resolved.launch_path == str(launch_alias)


def test_ensure_runtime_binary_reuses_versioned_managed_codex_layout(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "build" / "codex"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("#!/bin/sh\necho codex-reuse\n")
    source.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_artifacts, "_local_bin_dir", lambda: home / ".local" / "bin")
    monkeypatch.setattr(runtime_artifacts, "resolve_longhouse_home", lambda: home / ".longhouse")
    monkeypatch.setattr(runtime_artifacts, "_probe_executable_version", lambda path: "codex-cli 0.121.0")

    first = runtime_artifacts.ensure_runtime_binary(
        runtime_artifacts.RuntimeComponent.MANAGED_CODEX,
        source_override=str(source),
    )
    second = runtime_artifacts.ensure_runtime_binary(runtime_artifacts.RuntimeComponent.MANAGED_CODEX)

    assert first.installed_now is True
    assert second.installed_now is False
    assert second.path == str(home / ".local" / "bin" / "longhouse-codex")
    assert second.launch_path == str(
        home / ".longhouse" / "runtimes" / "codex" / "current" / runtime_artifacts.MANAGED_CODEX_LAUNCH_ALIAS
    )


def test_managed_codex_launcher_disables_upstream_update_checks(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "build" / "codex"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    source.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_artifacts, "_local_bin_dir", lambda: home / ".local" / "bin")
    monkeypatch.setattr(runtime_artifacts, "resolve_longhouse_home", lambda: home / ".longhouse")
    monkeypatch.setattr(runtime_artifacts, "_probe_executable_version", lambda path: "codex-cli 0.121.0")

    runtime_artifacts.ensure_runtime_binary(
        runtime_artifacts.RuntimeComponent.MANAGED_CODEX,
        source_override=str(source),
    )

    launcher = home / ".local" / "bin" / "longhouse-codex"
    completed = subprocess.run(
        [str(launcher), "--version"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout.splitlines() == [
        "-c",
        "check_for_update_on_startup=false",
        "--version",
    ]


def test_managed_codex_reinstall_from_current_launcher_skips_samefile_copy(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "build" / "codex"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    source.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_artifacts, "_local_bin_dir", lambda: home / ".local" / "bin")
    monkeypatch.setattr(runtime_artifacts, "resolve_longhouse_home", lambda: home / ".longhouse")
    monkeypatch.setattr(runtime_artifacts, "_probe_executable_version", lambda path: "codex-cli 0.121.0")

    first = runtime_artifacts.ensure_runtime_binary(
        runtime_artifacts.RuntimeComponent.MANAGED_CODEX,
        source_override=str(source),
    )
    second = runtime_artifacts.ensure_runtime_binary(
        runtime_artifacts.RuntimeComponent.MANAGED_CODEX,
        source_override=first.path,
        overwrite=True,
    )

    assert first.path == second.path
    assert second.installed_now is True

    launcher = home / ".local" / "bin" / "longhouse-codex"
    completed = subprocess.run(
        [str(launcher), "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert completed.stdout.splitlines() == [
        "-c",
        "check_for_update_on_startup=false",
        "--version",
    ]


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
        runtime_artifacts.RuntimeComponent.DESKTOP_APP,
        source_override=str(source_app),
    )

    destination = home / "Applications" / "Longhouse.app"
    assert destination.exists()
    assert (destination / "Contents" / "MacOS" / "Longhouse").read_text() == "app"
    assert result.path == str(destination)
    assert result.launch_path == str(destination / "Contents" / "MacOS" / "Longhouse")
    assert result.installed_now is True
    assert result.kind == runtime_artifacts.RuntimeArtifactKind.APP_BUNDLE


def test_ensure_runtime_artifact_replaces_disposable_smoke_app_bundle(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    existing_app = home / "Applications" / "Longhouse.app"
    existing_executable = existing_app / "Contents" / "MacOS" / "Longhouse"
    existing_executable.parent.mkdir(parents=True, exist_ok=True)
    existing_executable.write_text("old-app")
    existing_executable.chmod(0o755)
    (existing_app / "Contents" / "Info.plist").write_bytes(
        b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>ai.longhouse.app</string>
<key>CFBundleShortVersionString</key><string>0.0.0-smoke</string>
<key>CFBundleVersion</key><string>0.0.0-smoke</string>
</dict></plist>"""
    )

    downloads: list[tuple[runtime_artifacts.RuntimeComponent, str, Path]] = []

    def fake_install_remote(component: runtime_artifacts.RuntimeComponent, url: str, destination_path: Path) -> None:
        downloads.append((component, url, destination_path))
        executable = destination_path / "Contents" / "MacOS" / "Longhouse"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("fresh-app")
        executable.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_artifacts, "_local_applications_dir", lambda: home / "Applications")
    monkeypatch.setattr(runtime_artifacts, "_install_artifact_from_remote_source", fake_install_remote)
    monkeypatch.setattr(runtime_artifacts, "_platform_target", lambda: "darwin-arm64")
    monkeypatch.setattr(runtime_artifacts.metadata, "version", lambda package: "0.1.9")

    result = runtime_artifacts.ensure_runtime_artifact(runtime_artifacts.RuntimeComponent.DESKTOP_APP)

    expected_path = home / "Applications" / "Longhouse.app"
    assert result.path == str(expected_path)
    assert result.installed_now is True
    assert (expected_path / "Contents" / "MacOS" / "Longhouse").read_text() == "fresh-app"
    assert downloads == [
        (
            runtime_artifacts.RuntimeComponent.DESKTOP_APP,
            "https://github.com/cipher982/longhouse/releases/download/v0.1.9/Longhouse-macos-arm64.zip",
            expected_path,
        )
    ]


def test_ensure_runtime_artifact_reuses_non_disposable_app_bundle(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    existing_app = home / "Applications" / "Longhouse.app"
    existing_executable = existing_app / "Contents" / "MacOS" / "Longhouse"
    existing_executable.parent.mkdir(parents=True, exist_ok=True)
    existing_executable.write_text("stable-app")
    existing_executable.chmod(0o755)
    (existing_app / "Contents" / "Info.plist").write_bytes(
        b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>ai.longhouse.app</string>
<key>CFBundleShortVersionString</key><string>0.1.8</string>
<key>CFBundleVersion</key><string>0.1.8</string>
</dict></plist>"""
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_artifacts, "_local_applications_dir", lambda: home / "Applications")

    result = runtime_artifacts.ensure_runtime_artifact(runtime_artifacts.RuntimeComponent.DESKTOP_APP)

    assert result.path == str(existing_app)
    assert result.launch_path == str(existing_executable)
    assert result.installed_now is False
    assert result.source == "local-runtime-app"


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

    result = runtime_artifacts.ensure_runtime_artifact(runtime_artifacts.RuntimeComponent.DESKTOP_APP)

    expected_path = home / "Applications" / "Longhouse.app"
    assert result.path == str(expected_path)
    assert result.launch_path == str(expected_path / "Contents" / "MacOS" / "Longhouse")
    assert result.installed_now is True
    assert result.kind == runtime_artifacts.RuntimeArtifactKind.APP_BUNDLE
    assert downloads == [
        (
            runtime_artifacts.RuntimeComponent.DESKTOP_APP,
            "https://github.com/cipher982/longhouse/releases/download/v0.1.9/Longhouse-macos-arm64.zip",
            expected_path,
        )
    ]


def test_desktop_window_has_no_published_release_asset():
    with pytest.raises(RuntimeError, match="local-only runtime artifact"):
        runtime_artifacts._default_release_asset_url(runtime_artifacts.RuntimeComponent.DESKTOP_WINDOW)


def test_managed_codex_uses_release_url_when_override_missing(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()

    downloads: list[str] = []
    downloaded_binary = tmp_path / "downloaded-codex"
    downloaded_binary.write_text("#!/bin/sh\necho managed-codex\n")
    downloaded_binary.chmod(0o755)

    def fake_download(url: str) -> Path:
        downloads.append(url)
        return downloaded_binary

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_artifacts, "_local_bin_dir", lambda: home / ".local" / "bin")
    monkeypatch.setattr(runtime_artifacts, "resolve_longhouse_home", lambda: home / ".longhouse")
    monkeypatch.setattr(runtime_artifacts, "_download_to_temp_path", fake_download)
    monkeypatch.setattr(runtime_artifacts, "_verify_download_checksum", lambda url, downloaded_path: None)
    monkeypatch.setattr(runtime_artifacts, "_platform_target", lambda: "darwin-arm64")
    monkeypatch.setattr(runtime_artifacts.metadata, "version", lambda package: "0.1.9")
    monkeypatch.setattr(runtime_artifacts, "_probe_executable_version", lambda path: "codex-cli 0.121.0")

    result = runtime_artifacts.ensure_runtime_binary(runtime_artifacts.RuntimeComponent.MANAGED_CODEX)

    launcher = home / ".local" / "bin" / "longhouse-codex"
    install_root = home / ".longhouse" / "runtimes" / "codex"
    versions = list((install_root / "versions").iterdir())
    launch_alias = install_root / "current" / runtime_artifacts.MANAGED_CODEX_LAUNCH_ALIAS

    assert result.path == str(launcher)
    assert result.launch_path == str(launch_alias)
    assert result.installed_now is True
    assert len(versions) == 1
    assert launch_alias.is_symlink()
    assert os.readlink(launch_alias) == "longhouse-codex"
    assert downloads == ["https://github.com/cipher982/longhouse/releases/download/v0.1.9/longhouse-codex-darwin-arm64"]
    assert (versions[0] / "longhouse-codex").read_text() == "#!/bin/sh\necho managed-codex\n"


def test_ensure_runtime_artifact_falls_back_to_legacy_release_asset_when_canonical_zip_missing(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    attempted_urls: list[str] = []

    def fake_install_remote(component: runtime_artifacts.RuntimeComponent, url: str, destination_path: Path) -> None:
        attempted_urls.append(url)
        if url.endswith("Longhouse-macos-arm64.zip"):
            request = runtime_artifacts.httpx.Request("GET", url)
            response = runtime_artifacts.httpx.Response(404, request=request)
            raise runtime_artifacts.httpx.HTTPStatusError("missing", request=request, response=response)
        executable = destination_path / "Contents" / "MacOS" / "Longhouse"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("app")
        executable.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_artifacts, "_local_applications_dir", lambda: home / "Applications")
    monkeypatch.setattr(runtime_artifacts, "_install_artifact_from_remote_source", fake_install_remote)
    monkeypatch.setattr(runtime_artifacts, "_platform_target", lambda: "darwin-arm64")
    monkeypatch.setattr(runtime_artifacts.metadata, "version", lambda package: "0.1.9")

    result = runtime_artifacts.ensure_runtime_artifact(runtime_artifacts.RuntimeComponent.DESKTOP_APP)

    assert result.source == "https://github.com/cipher982/longhouse/releases/download/v0.1.9/longhouse-local-health-app-darwin-arm64.zip"
    assert attempted_urls == [
        "https://github.com/cipher982/longhouse/releases/download/v0.1.9/Longhouse-macos-arm64.zip",
        "https://github.com/cipher982/longhouse/releases/download/v0.1.9/longhouse-local-health-app-darwin-arm64.zip",
    ]


def test_verify_download_checksum_accepts_matching_release_asset(monkeypatch, tmp_path: Path):
    artifact_path = tmp_path / "longhouse-engine-linux-x64"
    artifact_path.write_bytes(b"engine-bytes")
    expected_checksum = hashlib.sha256(b"engine-bytes").hexdigest()

    monkeypatch.setattr(
        runtime_artifacts,
        "_load_release_checksums",
        lambda tag: {"longhouse-engine-linux-x64": expected_checksum},
    )

    runtime_artifacts._verify_download_checksum(
        "https://github.com/cipher982/longhouse/releases/download/v0.1.9/longhouse-engine-linux-x64",
        artifact_path,
    )


def test_verify_download_checksum_rejects_mismatched_release_asset(monkeypatch, tmp_path: Path):
    artifact_path = tmp_path / "longhouse-engine-linux-x64"
    artifact_path.write_bytes(b"wrong-bytes")
    expected_checksum = hashlib.sha256(b"engine-bytes").hexdigest()

    monkeypatch.setattr(
        runtime_artifacts,
        "_load_release_checksums",
        lambda tag: {"longhouse-engine-linux-x64": expected_checksum},
    )

    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        runtime_artifacts._verify_download_checksum(
            "https://github.com/cipher982/longhouse/releases/download/v0.1.9/longhouse-engine-linux-x64",
            artifact_path,
        )


def test_verify_download_checksum_skips_non_release_urls(monkeypatch, tmp_path: Path):
    artifact_path = tmp_path / "custom.zip"
    artifact_path.write_bytes(b"custom-bytes")

    monkeypatch.setattr(
        runtime_artifacts,
        "_load_release_checksums",
        lambda tag: pytest.fail("non-release URLs should not fetch release checksums"),
    )

    runtime_artifacts._verify_download_checksum("https://example.com/custom.zip", artifact_path)
