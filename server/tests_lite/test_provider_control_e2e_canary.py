from __future__ import annotations

import importlib.util
import json
import sqlite3
from types import ModuleType

import pytest

from zerg.qa.repo_root import default_repo_root


def _load_canary() -> ModuleType:
    path = default_repo_root() / "scripts" / "qa" / "provider-control-e2e-canary.py"
    spec = importlib.util.spec_from_file_location("provider_control_e2e_canary_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_opencode_real_run_environment_passes_only_its_explicit_token(monkeypatch, tmp_path) -> None:
    canary = _load_canary()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-token")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-cross-boundary")

    environment = canary._opencode_real_tool_env(tmp_path / "runtime")  # noqa: SLF001

    assert environment["OPENROUTER_API_KEY"] == "fixture-token"
    assert "UNRELATED_SECRET" not in environment
    assert environment["HOME"] == str(tmp_path / "runtime" / "home")
    assert environment["XDG_DATA_HOME"] == str(tmp_path / "runtime" / "data")


def test_opencode_native_model_evidence_reads_the_selected_native_message(tmp_path) -> None:
    canary = _load_canary()
    runtime = tmp_path / "opencode-runtime"
    database = runtime / "data" / "opencode" / "opencode.db"
    database.parent.mkdir(parents=True)
    record = {
        "id": "message-1",
        "sessionID": "session-1",
        "role": "assistant",
        "providerID": "openrouter",
        "modelID": "deepseek/deepseek-v4-flash",
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)"
        )
        connection.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("message-1", "session-1", 1, json.dumps(record)),
        )

    evidence = canary._opencode_native_model_evidence(runtime, session_ids=["session-1"])  # noqa: SLF001

    assert evidence is not None
    assert evidence["model"] == "openrouter/deepseek/deepseek-v4-flash"
    assert evidence["session_id"] == "session-1"
    assert evidence["path"] == str(database.resolve())
    assert len(evidence["sha256"]) == 64
    assert len(evidence["record_sha256"]) == 64


def test_opencode_native_model_evidence_rejects_conflicting_sqlite_session_bindings(tmp_path) -> None:
    canary = _load_canary()
    runtime = tmp_path / "opencode-runtime"
    database = runtime / "data" / "opencode" / "opencode.db"
    database.parent.mkdir(parents=True)
    record = {
        "id": "message-1",
        "sessionID": "payload-session",
        "role": "assistant",
        "providerID": "openrouter",
        "modelID": "deepseek/deepseek-v4-flash",
    }
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
        connection.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("message-1", "database-session", 1, json.dumps(record)),
        )

    evidence = canary._opencode_native_model_evidence(runtime, session_ids=["payload-session"])  # noqa: SLF001

    assert evidence is None


def test_opencode_native_model_evidence_uses_database_binding_when_payload_omits_session(tmp_path) -> None:
    canary = _load_canary()
    runtime = tmp_path / "opencode-runtime"
    database = runtime / "data" / "opencode" / "opencode.db"
    database.parent.mkdir(parents=True)
    record = {
        "id": "message-1",
        "role": "assistant",
        "providerID": "openrouter",
        "modelID": "deepseek/deepseek-v4-flash",
    }
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
        connection.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("message-1", "database-session", 1, json.dumps(record)),
        )

    evidence = canary._opencode_native_model_evidence(runtime, session_ids=["database-session"])  # noqa: SLF001

    assert evidence is not None
    assert evidence["model"] == "openrouter/deepseek/deepseek-v4-flash"
    assert evidence["session_id"] == "database-session"


def test_opencode_qualification_model_is_stable_and_overridable(monkeypatch) -> None:
    canary = _load_canary()
    monkeypatch.delenv("LONGHOUSE_OPENCODE_QUALIFICATION_MODEL", raising=False)
    assert canary._opencode_qualification_model() == "openrouter/~openai/gpt-mini-latest"  # noqa: SLF001

    monkeypatch.setenv("LONGHOUSE_OPENCODE_QUALIFICATION_MODEL", "deepseek/deepseek-v4-flash")
    assert canary._opencode_qualification_model() == "openrouter/deepseek/deepseek-v4-flash"  # noqa: SLF001

    monkeypatch.setenv("LONGHOUSE_OPENCODE_QUALIFICATION_MODEL", "openrouter/deepseek/fixture-model")
    assert canary._opencode_qualification_model() == "openrouter/deepseek/fixture-model"  # noqa: SLF001

    monkeypatch.setenv("LONGHOUSE_OPENCODE_QUALIFICATION_MODEL", "anthropic/claude-sonnet")
    with pytest.raises(ValueError, match="supported bare OpenRouter vendor"):
        canary._opencode_qualification_model()  # noqa: SLF001

    monkeypatch.setenv("LONGHOUSE_OPENCODE_QUALIFICATION_MODEL", "deepseek/")
    with pytest.raises(ValueError, match="supported bare OpenRouter vendor"):
        canary._opencode_qualification_model()  # noqa: SLF001

    monkeypatch.setenv("LONGHOUSE_OPENCODE_QUALIFICATION_MODEL", "openrouter/anthropic/claude-sonnet")
    with pytest.raises(ValueError, match="supported bare OpenRouter vendor"):
        canary._opencode_qualification_model()  # noqa: SLF001


def test_opencode_result_event_preserves_native_usage_cost_and_model_provenance() -> None:
    canary = _load_canary()
    events = [
        {
            "type": "text",
            "sessionID": "ses_fixture",
            "part": {"type": "text", "text": "LONGHOUSE_MARKER"},
        },
        {
            "type": "step_finish",
            "sessionID": "ses_fixture",
            "part": {
                "type": "step-finish",
                "tokens": {
                    "total": 7393,
                    "input": 7318,
                    "output": 11,
                    "reasoning": 0,
                    "cache": {"write": 0, "read": 64},
                },
                "cost": 0.001029392,
            },
        },
    ]

    result = canary._compact_opencode_result_event(  # noqa: SLF001
        events,
        marker="LONGHOUSE_MARKER",
        requested_model="openrouter/deepseek/deepseek-v4-flash",
    )

    assert result == {
        "type": "step_finish",
        "part_type": "step-finish",
        "session_id": "ses_fixture",
        "native_event_sha256": "23b71ffdd9b8ac9b0cd95dfc94b3699ebcf33acfbfd649bb71a5540d0302517d",
        "session_id_present": True,
        "result_exact_match": True,
        "accounting_status": "provider_reported",
        "accounting_status_source": "producer_observation_classification",
        "model": "openrouter/deepseek/deepseek-v4-flash",
        "model_source": "invocation",
        "usage": {
            "total": 7393,
            "input": 7318,
            "output": 11,
            "reasoning": 0,
            "cache.write": 0,
            "cache.read": 64,
        },
        "total_cost_usd": 0.001029392,
    }


def test_opencode_result_event_marks_missing_cost_without_calling_it_free() -> None:
    canary = _load_canary()
    result = canary._compact_opencode_result_event(  # noqa: SLF001
        [
            {
                "type": "step_finish",
                "sessionID": "ses_fixture",
                "part": {"type": "step-finish", "tokens": {"input": 12, "output": 3}},
            }
        ],
        marker="UNUSED",
        requested_model="openrouter/deepseek/deepseek-v4-flash",
    )

    assert result is not None
    assert result["accounting_status"] == "provider_reported_usage_cost_unavailable"
    assert result["accounting_status_source"] == "producer_observation_classification"
    assert "total_cost_usd" not in result


def test_opencode_part_shaped_result_preserves_the_native_event_type() -> None:
    canary = _load_canary()
    result = canary._compact_opencode_result_event(  # noqa: SLF001
        [
            {
                "type": "message.part.updated",
                "sessionID": "ses_fixture",
                "part": {"type": "step-finish", "tokens": {"input": 12, "output": 3}},
            }
        ],
        marker="UNUSED",
        requested_model="openrouter/deepseek/deepseek-v4-flash",
    )

    assert result is not None
    assert result["type"] == "message.part.updated"
    assert result["part_type"] == "step-finish"


def test_claude_synthetic_result_model_is_not_provider_identity() -> None:
    canary = _load_canary()

    result = canary._compact_claude_result_event(  # noqa: SLF001
        {
            "type": "result",
            "model": "<synthetic>",
            "result": "LONGHOUSE_MARKER",
        },
        marker="LONGHOUSE_MARKER",
    )

    assert result is not None
    assert "model" not in result
    assert "model_source" not in result


def test_work_root_is_separate_from_the_evidence_root(tmp_path, monkeypatch) -> None:
    """The defect: a provider's HOME lived inside the archived evidence tree.

    OpenCode npm-installs into that HOME, so ~250 MB of package cache became
    qualification evidence, twice per run, for every run ever made. The
    canary's product is its verdict; everything it needed to reach one is
    workspace and must not land where the factory archives forever.
    """

    canary = _load_canary()
    evidence_root = tmp_path / "evidence"
    artifact = evidence_root / "provider-control-e2e.json"

    observed: dict[str, object] = {}

    def fake_opencode_canary(args, provider_root):
        # Stand in for the real canary: write what a provider runtime would.
        observed["provider_root"] = provider_root
        (provider_root / "opencode-runtime" / "home" / ".npm" / "_cacache").mkdir(parents=True)
        (provider_root / "opencode-runtime" / "home" / ".npm" / "_cacache" / "blob").write_bytes(b"x" * 4096)
        return {"verdict": "green"}

    monkeypatch.setattr(canary, "run_opencode_canary", fake_opencode_canary)

    exit_code = canary.main(
        [
            "--provider",
            "opencode",
            "--evidence-root",
            str(evidence_root),
            "--artifact",
            str(artifact),
        ]
    )

    assert exit_code == 0
    payload = json.loads(artifact.read_text())
    assert payload["verdict"] == "green"

    # The verdict is the only thing in the evidence tree.
    written = sorted(p.relative_to(evidence_root).as_posix() for p in evidence_root.rglob("*") if p.is_file())
    assert written == ["provider-control-e2e.json"]

    # Nothing anywhere under the evidence root looks like a package cache.
    assert not any("_cacache" in p.as_posix() for p in evidence_root.rglob("*"))

    # The work root was scratch, outside the evidence tree, and is gone.
    provider_root = observed["provider_root"]
    assert evidence_root not in provider_root.parents
    assert not provider_root.exists()
    assert payload["work_root_retained"] is False


def test_keep_work_root_preserves_the_scratch_tree_for_debugging(tmp_path, monkeypatch) -> None:
    canary = _load_canary()
    evidence_root = tmp_path / "evidence"
    work_root = tmp_path / "work"

    def fake_opencode_canary(args, provider_root):
        (provider_root / "marker").write_text("kept")
        return {"verdict": "green"}

    monkeypatch.setattr(canary, "run_opencode_canary", fake_opencode_canary)

    canary.main(
        [
            "--provider",
            "opencode",
            "--evidence-root",
            str(evidence_root),
            "--work-root",
            str(work_root),
            "--artifact",
            str(evidence_root / "provider-control-e2e.json"),
        ]
    )

    assert (work_root / "opencode" / "marker").read_text() == "kept"
    payload = json.loads((evidence_root / "provider-control-e2e.json").read_text())
    assert payload["work_root_retained"] is True


def test_work_root_is_removed_even_when_a_canary_raises(tmp_path, monkeypatch) -> None:
    """A crashing canary must not leak a quarter-gigabyte of scratch."""

    canary = _load_canary()
    evidence_root = tmp_path / "evidence"
    seen: dict[str, object] = {}

    def exploding_canary(args, provider_root):
        seen["provider_root"] = provider_root
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(canary, "run_opencode_canary", exploding_canary)

    with pytest.raises(RuntimeError):
        canary.main(
            [
                "--provider",
                "opencode",
                "--evidence-root",
                str(evidence_root),
                "--artifact",
                str(evidence_root / "provider-control-e2e.json"),
            ]
        )

    assert not seen["provider_root"].exists()
