"""The factory producer that carries the served-state proof.

`provider_console_lifecycle` drives a real turn and judges the machine.
`product_console_lifecycle` judges the served contract but supplies its own
terminal, with no provider. This producer is the one that does both, which is
the gap the ten-hour wedge fell through.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.qa import console_served_state as producer  # noqa: E402


def test_the_registration_is_a_longhouse_product_subject_with_live_evidence():
    registration = producer.REGISTRATION.to_dict()
    # Not a provider_release: the subject under test is Longhouse, and the
    # provider is the instrument. Getting this wrong qualifies the wrong thing.
    assert registration["subject_kind"] == "longhouse_product"
    # Nothing here is reconstructed from fixtures.
    assert registration["evidence_classes"] == ["live_token"]
    assert registration["modes"] == ["console"]
    assert registration["producer_revision"] == 4


def test_the_vehicle_is_exact_without_changing_the_subject():
    registration = producer.REGISTRATION.to_dict()
    assert registration["vehicle_provider"] == "codex"
    assert registration["providers"] == []


def test_the_registration_declares_no_provider_even_though_one_runs():
    # A provider does run -- it is the instrument that produces a Console turn.
    # But the subject under test is Longhouse, and the factory refuses a
    # longhouse_product observation that carries a provider
    # (provider_factory/cases.py: "Longhouse product case observation carries a
    # provider"). Declaring the vehicle here would make every published case
    # invalid, so the runtime choice set and the declared subject are
    # deliberately different things.
    assert producer.REGISTRATION.to_dict()["providers"] == []


def test_the_registration_binds_the_vehicle_and_runtime_host_authorities():
    # The proof is what a viewer is served, so the producer needs the Runtime
    # Host credential. It is bound once for the producer rather than per
    # provider, because the subject is the Runtime Host's served contract and
    # not any one provider's release.
    assert producer.REGISTRATION.to_dict()["credential_binding_ids"] == [
        "codex_provider_token",
        "runtime_host_control",
    ]
    assert "vehicle_dispatch_receipt" in producer.REGISTRATION.to_dict()["required_artifacts"]
    assert "canary_session_hidden" in producer.REGISTRATION.to_dict()["required_cleanup"]


def test_the_oracle_points_at_the_shared_core():
    registration = producer.REGISTRATION.to_dict()
    assert registration["oracle_source"] == "server/zerg/qa/console_served_state_core.py"
    assert registration["oracle_entrypoint"] == "settlement_state"


def test_a_clean_report_passes_both_assertions():
    report = {"first_live_frame_s": 0.9, "frame_count": 24, "marker_served": True, "settle_latency_s": 0.3}
    assert producer.assertions_from_report(report) == {
        producer.ASSERTION_LIVE: True,
        producer.ASSERTION_SETTLED: True,
    }


def test_the_wedge_fails_the_settlement_cell_only():
    # The incident: the reply reached the viewer and the state never settled.
    report = {"first_live_frame_s": 0.9, "frame_count": 24, "marker_served": True, "settle_latency_s": None}
    assertions = producer.assertions_from_report(report)
    assert assertions[producer.ASSERTION_LIVE] is True
    assert assertions[producer.ASSERTION_SETTLED] is False


def test_no_live_frames_fails_the_delivery_cell():
    # The iOS symptom: the turn ran, nothing streamed.
    report = {"first_live_frame_s": None, "frame_count": 0, "marker_served": True, "settle_latency_s": 0.3}
    assert producer.assertions_from_report(report)[producer.ASSERTION_LIVE] is False


def test_settlement_needs_a_served_reply_not_just_a_latency():
    # A turn that produced nothing has nothing to settle from, so a stray
    # latency must not be read as proof.
    report = {"first_live_frame_s": 0.9, "frame_count": 5, "marker_served": False, "settle_latency_s": 0.3}
    assert producer.assertions_from_report(report)[producer.ASSERTION_SETTLED] is False


def test_the_factory_product_argv_parses_without_a_provider():
    # The auxiliary vehicle is explicit, but it is not a provider subject
    # argument: the factory supplies its exact binary/version/model separately.
    args = producer._parser().parse_args(["--evidence-root", "/tmp/evidence"])
    assert not hasattr(args, "provider")


def test_vehicle_dispatch_binds_exact_binary_model_and_run(tmp_path):
    provider_bin = tmp_path / "codex"
    provider_bin.write_text("binary", encoding="utf-8")
    claim = {
        "session_id": "session-1",
        "run_id": "run-1",
        "state": "terminal",
        "result": {
            "terminal_state": "run_completed",
            "argv": [str(provider_bin), "exec", "--config", 'model="gpt-5.6-sol"'],
        },
    }

    receipt = producer._vehicle_dispatch_receipt(
        claim,
        provider_bin=provider_bin,
        model="gpt-5.6-sol",
        session_id="session-1",
        run_id="run-1",
    )

    assert receipt["status"] == "pass"
    assert receipt["binary_bound"] is True
    assert receipt["model_bound"] is True
    assert receipt["identity_bound"] is True


def test_shared_oracle_binds_the_vehicle_model_and_exposes_created_session(monkeypatch, tmp_path):
    from zerg.qa import console_served_state_core as core

    requests = []

    class _Client:
        def __init__(self, _api_url, _token):
            pass

        def request(self, method, path, payload=None, **_kwargs):
            requests.append((method, path, payload))
            return {"session_id": "session-1"}

    monkeypatch.setattr(core, "_defaults", lambda: ("https://runtime.example", "token"))
    monkeypatch.setattr(core, "Client", _Client)
    monkeypatch.setattr(
        core,
        "_observe_turn",
        lambda _client, _args, report, _session_id, _marker: report,
    )
    created = []
    args = type(
        "Args",
        (),
        {
            "provider": "codex",
            "device_id": "factory-machine",
            "cwd": str(tmp_path),
            "api_url": None,
            "model": "gpt-5.6-sol",
            "drop_terminal": False,
        },
    )()

    core.run(args, on_session_created=created.append)

    assert created == ["session-1"]
    assert requests[0][2]["model"] == "gpt-5.6-sol"


def test_shared_oracle_waits_for_machine_adapter_registration(monkeypatch):
    from zerg.qa import console_served_state_core as core

    attempts = 0

    class _Client:
        def request(self, method, path, payload=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise core.ApiError(409, "adapter_unavailable")
            return {"session_id": "session-1"}

    monkeypatch.setattr(core.time, "sleep", lambda _seconds: None)
    created = core._create_session(_Client(), {"provider": "codex"}, timeout=1)

    assert created == {"session_id": "session-1"}
    assert attempts == 2


def test_the_result_identity_carries_no_provider_but_still_names_the_vehicle(monkeypatch, tmp_path):
    # A longhouse_product result that names a provider is compared against a
    # contract pinning `provider: null` and rejected as an inadmissible result
    # -- indistinguishable, from the outside, from a malformed producer. The
    # vehicle still has to be recoverable, so it lives beside the identity and
    # in the observation rather than inside what the result claims to be.
    report = {"first_live_frame_s": 0.9, "frame_count": 24, "marker_served": True, "settle_latency_s": 0.3}
    from zerg.qa import console_served_state_core as core

    # core.run stamps the vehicle onto the report it returns, so the stub does
    # too -- otherwise this asserts against the stub instead of the producer.
    monkeypatch.setattr(
        core,
        "run",
        lambda args, **_kwargs: {**report, "provider": args.provider},
    )
    result = producer.run_console_served_state(tmp_path, provider="codex", device_id="d", cwd="/tmp/x")
    assert result["provider"] is None
    assert result["vehicle_provider"] == "codex"
    assert result["observation"]["provider"] == "codex"
    assert result["status"] == "pass"


def test_failure_result_retains_vehicle_identity_and_false_verdicts():
    result = producer._failure_result(
        model="gpt-5.6-sol",
        provider="codex",
        device_id="factory-machine",
        session_id="session-1",
        failure=RuntimeError("runtime unavailable"),
    )

    assert result["provider"] is None
    assert result["vehicle_provider"] == "codex"
    assert result["vehicle_qualification_model"] == "gpt-5.6-sol"
    assert result["assertions"] == {
        producer.ASSERTION_LIVE: False,
        producer.ASSERTION_SETTLED: False,
    }
