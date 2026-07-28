from zerg.qa.provider_release_identity import IdentityObservation
from zerg.qa.provider_release_identity import identity_oracle
from zerg.services.provider_capability_proof import AssertionOutcome


def _observation(**overrides) -> IdentityObservation:
    defaults = dict(
        pre_execution_identity="sha256:abc",
        post_execution_identity="sha256:abc",
        process_returned=True,
        returncode=0,
        reported_version="1.2.3",
        expected_provider_version="1.2.3",
    )
    defaults.update(overrides)
    return IdentityObservation(**defaults)


def test_matching_identity_and_version_both_pass() -> None:
    identity_outcome, version_outcome = identity_oracle(_observation())
    assert identity_outcome == AssertionOutcome.PASS
    assert version_outcome == AssertionOutcome.PASS


def test_identity_drift_between_pre_and_post_execution_is_infrastructure_error() -> None:
    identity_outcome, _ = identity_oracle(_observation(post_execution_identity="sha256:different"))
    assert identity_outcome == AssertionOutcome.INFRASTRUCTURE_ERROR


def test_process_did_not_return_is_infrastructure_error_for_version() -> None:
    _, version_outcome = identity_oracle(_observation(process_returned=False, returncode=None))
    assert version_outcome == AssertionOutcome.INFRASTRUCTURE_ERROR


def test_nonzero_returncode_is_infrastructure_error_for_version() -> None:
    _, version_outcome = identity_oracle(_observation(returncode=1))
    assert version_outcome == AssertionOutcome.INFRASTRUCTURE_ERROR


def test_version_mismatch_is_semantic_fail() -> None:
    _, version_outcome = identity_oracle(_observation(reported_version="9.9.9"))
    assert version_outcome == AssertionOutcome.SEMANTIC_FAIL
