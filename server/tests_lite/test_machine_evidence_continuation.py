from datetime import UTC
from datetime import datetime
from datetime import timedelta

from zerg.machine_evidence import canonical_evidence_hash
from zerg.machine_evidence import validate_machine_evidence_identities
from zerg.routers.heartbeat import MachineEvidenceIn


def test_retained_contract_is_reducer_grade_continuation_evidence() -> None:
    now = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    value = {
        "authority_class": "retained_launch_contract",
        "provider": "claude",
        "session_id": "11111111-1111-4111-8111-111111111111",
        "provider_session_id": "22222222-2222-4222-8222-222222222222",
        "cwd": "/repo",
        "contract_state": "valid",
        "observed_at": now.isoformat(),
        "valid_until": (now + timedelta(minutes=20)).isoformat(),
        "source": "managed_resume_contract_scan",
        "raw_locator": "/state/contracts/claude/session.json",
    }
    evidence_hash = canonical_evidence_hash(value)
    evidence = {
        "schema_version": 3,
        "observed_at": now.isoformat(),
        "identities": [
            {
                "fact_family": "continuation",
                "fact_index": 0,
                "subject_key": "resume:11111111-1111-4111-8111-111111111111",
                "source": "managed_resume_contract_scan",
                "source_epoch": "contract-v1",
                "source_seq": None,
                "sequenced": False,
                "dedupe_key": "a" * 64,
                "evidence_hash": evidence_hash,
            }
        ],
        "run": [],
        "process": [],
        "activity": [],
        "control": [],
        "transcript": [],
        "process_snapshot_scopes": [],
        "readiness": [],
        "continuation": [value],
    }

    parsed = MachineEvidenceIn.model_validate(evidence)
    [identity] = validate_machine_evidence_identities(parsed.model_dump(mode="json", exclude_none=True))
    assert identity.family == "continuation"
    assert identity.value["contract_state"] == "valid"
