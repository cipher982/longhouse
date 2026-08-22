"""Project attribution never invents a name for a generic container directory.

The live corpus carried 921 OpenCode sessions filed under a project literally
called `workspace`, plus 27 under `w`, all of them provider-factory or canary
run directories. A server-side guard already existed and let them through
because it also required the cwd basename to match and no git remote to be
present. These pin the repaired behaviour.
"""

from datetime import datetime
from datetime import timezone

from zerg.services.agents.models import SessionIngest
from zerg.services.agents.store import _normalize_ingested_project


def _ingest(**overrides) -> SessionIngest:
    payload = {
        "provider": "opencode",
        "environment": "development",
        "device_id": "cinder",
        "started_at": datetime(2026, 8, 22, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return SessionIngest(**payload)


def test_generic_container_labels_are_refused_regardless_of_context():
    for label in ("workspace", "ws", "w"):
        # Bare.
        assert _normalize_ingested_project(_ingest(project=label)) is None
        # Inside a real repository with a remote — the two conditions the old
        # guard required, and the shape that let the corpus through.
        assert (
            _normalize_ingested_project(
                _ingest(
                    project=label,
                    cwd="/Users/davidrose/git/g55/run/evidence",
                    git_repo="git@github.com:cipher982/g55.git",
                )
            )
            is None
        )


def test_real_project_names_survive():
    assert _normalize_ingested_project(_ingest(project="g55")) == "g55"
    assert _normalize_ingested_project(_ingest(project="  longhouse  ")) == "longhouse"
    assert _normalize_ingested_project(_ingest(project=None)) is None
    assert _normalize_ingested_project(_ingest(project="   ")) is None
