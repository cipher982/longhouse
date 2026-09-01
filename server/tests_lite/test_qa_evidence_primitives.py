"""The shared evidence primitives in zerg/qa/ stay shared.

`_now`, `_sha256` and `_artifact_manifest` were re-typed in 17 modules with
silent divergence -- three spellings of the timestamp, five of the digest, three
of the manifest -- and two of the spellings produced *different evidence bytes*
than their neighbours. The identical ones now live once, on the already-pinned
`provider_release_identity` module. These tests hold that line and record the
divergences that were deliberately left alone.
"""

from __future__ import annotations

import ast
import hashlib
import pathlib
import re
from datetime import UTC
from datetime import datetime

import pytest

import zerg.qa
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import now
from zerg.qa.provider_release_identity import sha256_file

QA_DIR = pathlib.Path(zerg.qa.__file__).parent

# Digest helpers that deliberately emit *bare hex* rather than the `sha256:`
# prefix the evidence manifests use. Unifying them would rewrite the evidence
# bytes of the conversation-reset and interaction-probe lanes, so they stay.
BARE_HEX_SHA256_MODULES = {"claude_conversation_reset.py", "provider_interaction_probe.py"}
# Digest helpers over a non-Path subject (bytes / str), which is a different
# function that happens to share a name.
NON_PATH_SHA256_MODULES = {"cursor_visibility_evidence.py"}
# Timestamp helpers that emit `+00:00` instead of `Z`. Same instant, different
# serialized evidence, so migrating them is an evidence-format change.
OFFSET_SUFFIX_NOW_MODULES = {"cursor_helm_gate0.py", "cursor_helm_product_e2e.py"}


def _top_level_defs(name: str) -> dict[str, ast.FunctionDef]:
    found = {}
    for path in sorted(QA_DIR.glob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                found[path.name] = node
    return found


def test_artifact_manifest_is_defined_exactly_once() -> None:
    assert _top_level_defs("_artifact_manifest") == {}


def test_only_the_documented_divergent_now_helpers_remain() -> None:
    assert set(_top_level_defs("_now")) == OFFSET_SUFFIX_NOW_MODULES


def test_only_the_documented_divergent_sha256_helpers_remain() -> None:
    assert set(_top_level_defs("_sha256")) == BARE_HEX_SHA256_MODULES | NON_PATH_SHA256_MODULES


def test_shared_now_is_the_z_suffixed_spelling() -> None:
    stamp = now()
    assert stamp.endswith("Z")
    assert "+00:00" not in stamp
    # Round-trips to the instant it claims to be.
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert abs((datetime.now(UTC) - parsed).total_seconds()) < 60


def test_shared_sha256_file_keeps_the_manifest_prefix(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "evidence.bin"
    payload = b"\x00\xff" * 5000
    target.write_bytes(payload)
    assert sha256_file(target) == f"sha256:{hashlib.sha256(payload).hexdigest()}"


def test_shared_sha256_file_streams_large_inputs_identically(tmp_path: pathlib.Path) -> None:
    """The two collapsed spellings differed only in chunked vs whole-file reads."""
    target = tmp_path / "big.bin"
    payload = b"x" * (1024 * 1024 * 2 + 17)  # spans the 1 MiB chunk boundary
    target.write_bytes(payload)
    assert sha256_file(target) == f"sha256:{hashlib.sha256(payload).hexdigest()}"


def test_shared_artifact_manifest_excludes_the_result_envelope(tmp_path: pathlib.Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "a.txt").write_bytes(b"hello")
    (tmp_path / "nested" / "b.log").write_bytes(b"world!")
    (tmp_path / "result.json").write_text("{}")

    manifest = artifact_manifest(tmp_path)

    assert [row["path"] for row in manifest] == ["a.txt", "nested/b.log"]
    assert [row["size"] for row in manifest] == [5, 6]
    assert manifest[0]["sha256"] == f"sha256:{hashlib.sha256(b'hello').hexdigest()}"


def test_no_module_reaches_through_to_a_migrated_private() -> None:
    """The reach-through imports these helpers had are gone, not relocated."""
    pattern = re.compile(r"from zerg\.qa\.\w+ import (_now|_artifact_manifest)\b")
    offenders = [path.name for path in QA_DIR.glob("*.py") if pattern.search(path.read_text(encoding="utf-8"))]
    assert offenders == []


@pytest.mark.parametrize("module_name", sorted(BARE_HEX_SHA256_MODULES))
def test_bare_hex_sha256_divergence_is_real_and_intentional(module_name: str) -> None:
    """Guard the reason these were not collapsed: they emit a different string."""
    source = (QA_DIR / module_name).read_text(encoding="utf-8")
    body = source[source.index("def _sha256(") :]
    assert "sha256:" not in body[: body.index("\n\n\n")]
