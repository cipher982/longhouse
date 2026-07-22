from pathlib import Path

from zerg.services.provider_capability_local_proof import collect_local_capability_proofs
from zerg.services.provider_capability_local_proof import executable_identity


def test_executable_identity_hashes_resolved_payload(tmp_path: Path) -> None:
    binary = tmp_path / "provider"
    binary.write_bytes(b"provider payload")

    identity = executable_identity(str(binary))

    assert identity is not None and identity.startswith("sha256:") and len(identity) == 71


def test_local_reader_reports_empty_provider_roots_without_creating_them(tmp_path: Path) -> None:
    records, summary = collect_local_capability_proofs(tmp_path)

    assert all(not provider_records for provider_records in records.values())
    assert all(info["state"] == "missing" for info in summary["providers"].values())
    assert not (tmp_path / "provider-capability-proofs").exists()
