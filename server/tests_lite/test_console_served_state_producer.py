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
    # A hardcoded tuple is how a provider silently escapes coverage.
    assert list(producer.PROVIDERS) == console_providers()
    assert producer.REGISTRATION.to_dict()["providers"] == console_providers()


def test_every_provider_binds_runtime_host_control():
    # The proof is what a viewer is served, so the producer needs the Runtime
    # Host credential as well as the provider's own.
    bindings = producer.REGISTRATION.to_dict()["credential_binding_ids_by_provider"]
    for provider in producer.PROVIDERS:
        assert "runtime_host_control" in bindings[provider], provider
        assert f"{provider}_provider_token" in bindings[provider], provider


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
