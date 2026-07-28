import pytest

from zerg.qa.run_evidence_index import InterventionLogEntry
from zerg.qa.run_evidence_index import RawArtifact
from zerg.qa.run_evidence_index import RunEvidenceIndex


def _index(**overrides) -> RunEvidenceIndex:
    defaults = dict(
        schema_version=1,
        plan_cell={"provider": "codex", "trigger": "release_poll"},
        intervention_log=(),
        raw_artifacts=(),
        build_provenance="staged_release",
        sandbox_receipt=None,
        longhouse_git_sha="a" * 40,
    )
    defaults.update(overrides)
    return RunEvidenceIndex(**defaults)


def test_round_trips_through_dict() -> None:
    index = _index(
        intervention_log=(InterventionLogEntry(action="interrupt", monotonic_timestamp=1.5, bound_turn_id="t1"),),
        raw_artifacts=(RawArtifact(path="proof-bundle.json", sha256="b" * 64),),
        control_plane_git_sha="c" * 40,
    )
    restored = RunEvidenceIndex.from_dict(index.to_dict())
    assert restored == index


def test_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValueError):
        _index(schema_version=2)


def test_rejects_missing_longhouse_git_sha() -> None:
    with pytest.raises(ValueError):
        _index(longhouse_git_sha="")


def test_sandbox_receipt_and_control_plane_sha_are_optional() -> None:
    index = _index()
    assert index.sandbox_receipt is None
    assert index.control_plane_git_sha is None
    assert index.to_dict()["sandbox_receipt"] is None
