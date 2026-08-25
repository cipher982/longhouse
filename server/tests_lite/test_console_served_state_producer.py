"""The factory producer that carries the served-state proof.

`provider_console_lifecycle` drives a real turn and judges the machine.
`product_console_lifecycle` judges the served contract but supplies its own
terminal, with no provider. This producer is the one that does both, which is
the gap the ten-hour wedge fell through.
"""

from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.qa import console_served_state as producer  # noqa: E402
from zerg.qa.console_served_state_core import console_providers  # noqa: E402


def test_the_registration_is_a_longhouse_product_subject_with_live_evidence():
    registration = producer.REGISTRATION.to_dict()
    # Not a provider_release: the subject under test is Longhouse, and the
    # provider is the instrument. Getting this wrong qualifies the wrong thing.
    assert registration["subject_kind"] == "longhouse_product"
    # Nothing here is reconstructed from fixtures.
    assert registration["evidence_classes"] == ["live_token"]
    assert registration["modes"] == ["console"]


def test_the_provider_set_is_derived_from_the_schema_not_restated():
    # A hardcoded tuple is how a provider silently escapes coverage. This is the
    # set the producer may choose a vehicle from at runtime.
    assert list(producer.PROVIDERS) == console_providers()


def test_the_registration_declares_no_provider_even_though_one_runs():
    # A provider does run -- it is the instrument that produces a Console turn.
    # But the subject under test is Longhouse, and the factory refuses a
    # longhouse_product observation that carries a provider
    # (provider_factory/cases.py: "Longhouse product case observation carries a
    # provider"). Declaring the vehicle here would make every published case
    # invalid, so the runtime choice set and the declared subject are
    # deliberately different things.
    assert producer.REGISTRATION.to_dict()["providers"] == []


def test_the_registration_binds_runtime_host_control():
    # The proof is what a viewer is served, so the producer needs the Runtime
    # Host credential. It is bound once for the producer rather than per
    # provider, because the subject is the Runtime Host's served contract and
    # not any one provider's release.
    assert producer.REGISTRATION.to_dict()["credential_binding_ids"] == ["runtime_host_control"]


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
    # The factory dispatches a longhouse_product cell as exactly this argv
    # (provider_factory/assurance.py: `[python, oracle_source,
    # "--evidence-root", dir]`) -- there is no provider to pass, because the
    # contract pins `provider: null`. Requiring one here exited 2 before any
    # evidence was written, which is unreadable from the outside: the factory
    # reports "producer exited 2 without a result artifact" and cannot say
    # whether Longhouse failed or the harness did.
    args = producer._parser().parse_args(["--evidence-root", "/tmp/evidence"])
    assert args.provider is None


class _FakeClient:
    """Stands in for the core Client, returning one machine directory payload."""

    def __init__(self, machines):
        self._machines = machines
        self.calls = []

    def request(self, method, path, *a, **k):
        self.calls.append((method, path))
        return {"machines": self._machines}


def test_the_vehicle_is_the_adapter_the_machine_actually_advertises():
    # The schema declares several console adapters; this machine announces only
    # the second one. Picking by declaration order chose an adapter the agent
    # could not start and died on a 409.
    providers = console_providers()
    assert len(providers) >= 2, providers
    wanted = providers[1]
    client = _FakeClient([{"device_id": "factory-machine", "online": True, "supports": [f"{wanted}.turn_start"]}])
    assert producer.select_vehicle(client, "factory-machine") == ("factory-machine", wanted)
    assert client.calls == [("GET", "/api/agents/machines")]


def test_the_adapter_is_waited_for_rather_than_read_once(monkeypatch):
    """The Machine Agent announces turn_start after its channel connects.

    provider_console_lifecycle retries POST /api/agents/sessions for 45s on
    adapter_unavailable for exactly this reason. Reading the directory once
    right after launch saw an agent advertising nothing and gave up.
    """
    wanted = console_providers()[0]
    frames = [
        {"device_id": "factory-machine", "online": False, "supports": []},
        {"device_id": "factory-machine", "online": True, "supports": []},
        {"device_id": "factory-machine", "online": True, "supports": [f"{wanted}.turn_start"]},
    ]

    class _Waking:
        def __init__(self):
            self.reads = 0

        def request(self, method, path, *a, **k):
            frame = frames[min(self.reads, len(frames) - 1)]
            self.reads += 1
            return {"machines": [frame]}

    monkeypatch.setattr(producer.time, "sleep", lambda _s: None)
    client = _Waking()
    assert producer.select_vehicle(client, "factory-machine") == ("factory-machine", wanted)
    assert client.reads == 3


def test_a_machine_advertising_no_console_adapter_says_what_it_did_advertise(monkeypatch):
    monkeypatch.setattr(producer.time, "sleep", lambda _s: None)
    client = _FakeClient([{"device_id": "factory-machine", "online": True, "supports": ["codex.archive_backlog"]}])
    with pytest.raises(RuntimeError) as excinfo:
        producer.select_vehicle(client, "factory-machine", timeout=0)
    assert "codex.archive_backlog" in str(excinfo.value)


def test_an_offline_machine_is_named_as_such(monkeypatch):
    monkeypatch.setattr(producer.time, "sleep", lambda _s: None)
    client = _FakeClient([{"device_id": "factory-machine", "online": False, "supports": []}])
    with pytest.raises(RuntimeError) as excinfo:
        producer.select_vehicle(client, "factory-machine", timeout=0)
    # The whole directory is reported, so "which machine and what was wrong
    # with it" is answerable from the failure artifact alone.
    assert "factory-machine: offline" in str(excinfo.value)


def test_an_unenrolled_device_lists_the_machines_that_are_present(monkeypatch):
    monkeypatch.setattr(producer.time, "sleep", lambda _s: None)
    client = _FakeClient([{"device_id": "some-other-box", "online": True, "supports": []}])
    with pytest.raises(RuntimeError) as excinfo:
        producer.select_vehicle(client, "factory-machine", timeout=0)
    assert "some-other-box" in str(excinfo.value)


def test_the_machine_is_chosen_when_none_is_named(monkeypatch):
    """The factory names no device, because the subject is Longhouse.

    The old default was the literal "factory-machine", which exists only as a
    product_console_lifecycle fixture. The live directory had seven real
    enrollments and none of them was that, so the lookup could never match and
    the cell failed 45s later against a device that does not exist.
    """
    monkeypatch.setattr(producer.time, "sleep", lambda _s: None)
    wanted = console_providers()[0]
    client = _FakeClient(
        [
            {"device_id": "cube", "online": False, "supports": [f"{wanted}.turn_start"]},
            {"device_id": "cinder", "online": True, "supports": ["something.else"]},
            {"device_id": "provider-factory-resume", "online": True, "supports": [f"{wanted}.turn_start"]},
        ]
    )
    assert producer.select_vehicle(client) == ("provider-factory-resume", wanted)


def test_an_empty_directory_says_so(monkeypatch):
    monkeypatch.setattr(producer.time, "sleep", lambda _s: None)
    with pytest.raises(RuntimeError) as excinfo:
        producer.select_vehicle(_FakeClient([]), timeout=0)
    assert "no machines enrolled" in str(excinfo.value)


def test_advertised_console_providers_ignores_non_turn_start_capabilities():
    providers = console_providers()
    supports = [f"{providers[0]}.archive_backlog", f"{providers[0]}.turn_start"]
    assert producer.advertised_console_providers(supports) == [providers[0]]
    assert producer.advertised_console_providers(None) == []


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
    monkeypatch.setattr(core, "run", lambda args: {**report, "provider": args.provider})
    result = producer.run_console_served_state(tmp_path, provider="codex", device_id="d", cwd="/tmp/x")
    assert result["provider"] is None
    assert result["vehicle_provider"] == "codex"
    assert result["observation"]["provider"] == "codex"
    assert result["status"] == "pass"
