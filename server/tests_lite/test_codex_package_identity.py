from pathlib import Path

import pytest

from zerg.qa import codex_helm_interrupt
from zerg.qa import codex_release_identity as identity_bridge


def _package(root: Path, extra: tuple[str, ...] = ()) -> Path:
    for name in (*sorted(codex_helm_interrupt.PACKAGE_MEMBERS), *extra):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
        path.chmod(0o700)
    return root / "bin/codex"


def test_voice_resource_subtree_is_admitted_and_hashed(tmp_path: Path) -> None:
    plain = _package(tmp_path / "plain")
    _, plain_digest, plain_identities = codex_helm_interrupt._package_identity(str(tmp_path / "plain"), plain)  # noqa: SLF001

    voiced = _package(tmp_path / "voiced", extra=("codex-resources/voice/manifest.json", "codex-resources/voice/lib/libz.so.1"))
    _, voiced_digest, voiced_identities = codex_helm_interrupt._package_identity(str(tmp_path / "voiced"), voiced)  # noqa: SLF001

    assert set(plain_identities) == codex_helm_interrupt.PACKAGE_MEMBERS
    assert "codex-resources/voice/manifest.json" in voiced_identities
    assert voiced_digest != plain_digest


def test_unknown_member_outside_voice_subtree_is_rejected(tmp_path: Path) -> None:
    binary = _package(tmp_path / "pkg", extra=("codex-resources/other/tool",))
    with pytest.raises(identity_bridge.RequestError, match="unexpected=\\['codex-resources/other/tool'\\]"):
        codex_helm_interrupt._package_identity(str(tmp_path / "pkg"), binary)  # noqa: SLF001
