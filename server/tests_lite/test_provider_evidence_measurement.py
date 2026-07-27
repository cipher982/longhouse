from __future__ import annotations

import json
from pathlib import Path

from zerg.qa.provider_evidence_measurement import canonical_digest_v1
from zerg.qa.provider_evidence_measurement import measure_evidence_package
from zerg.qa.provider_evidence_measurement import structural_fingerprint_v1


def _capture(root: Path, *, session_id: str, thread_id: str, marker: str, tool_name: str) -> dict:
    return {
        "package": {"root": str(root), "generated_at": "2026-07-27T12:00:00Z"},
        "session_id": session_id,
        "thread_id": thread_id,
        "state_path": str(root / "state" / session_id),
        "canary_marker": marker,
        "events": [
            {
                "type": "tool_call",
                "tool_name": tool_name,
                "arguments": {"query": marker},
            }
        ],
    }


def test_canonical_digest_is_stable_across_package_roots() -> None:
    first_root = Path("/tmp/first-package")
    second_root = Path("/private/tmp/second-package")
    first = _capture(first_root, session_id="session-a", thread_id="thread-a", marker="marker-a", tool_name="search")
    second = _capture(second_root, session_id="session-a", thread_id="thread-a", marker="marker-a", tool_name="search")

    assert canonical_digest_v1(first, package_root=first_root) == canonical_digest_v1(
        second,
        package_root=second_root,
    )


def test_live_value_churn_moves_canonical_digest_but_not_structure() -> None:
    root = Path("/tmp/package")
    first = _capture(root, session_id="session-a", thread_id="thread-a", marker="marker-a", tool_name="search")
    second = _capture(root, session_id="session-b", thread_id="thread-b", marker="marker-b", tool_name="search")

    assert canonical_digest_v1(first, package_root=root) != canonical_digest_v1(second, package_root=root)
    assert structural_fingerprint_v1(first) == structural_fingerprint_v1(second)


def test_a_different_tool_call_moves_the_structural_fingerprint() -> None:
    root = Path("/tmp/package")
    first = _capture(root, session_id="session-a", thread_id="thread-a", marker="marker-a", tool_name="search")
    second = _capture(root, session_id="session-a", thread_id="thread-a", marker="marker-a", tool_name="write_file")

    assert structural_fingerprint_v1(first) != structural_fingerprint_v1(second)


def test_evidence_package_measurement_is_recorded_but_has_no_skip_authority(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "manifest.json").write_text(
        json.dumps({"artifact_kind": "evidence", "root": str(package)}),
        encoding="utf-8",
    )

    measurement = measure_evidence_package(package)

    assert measurement["decision_role"] == "observation_only"
    assert measurement["skip_authority"] is False
    assert measurement["input_files"] == ["manifest.json"]
