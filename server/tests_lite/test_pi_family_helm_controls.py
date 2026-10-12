"""The Pi-family Helm negative controls that end at their target, run as the factory runs them, against the fake.

A no-op send or terminate leaves nothing after it that can run, so the producer rules on the control from where it
stopped. The fake launcher (``_pi_family_helm_fake``) does what the faulted engine does and leaves its receipt.
These are apart from the lifecycle fixture because the suite gives a worker whole files, and each run here waits out
what its fault prevents.
"""

from __future__ import annotations

import json

import pytest

from tests_lite import _pi_family_helm_fake as fake
from zerg.qa import pi_family_helm as helm
from zerg.qa.pi_family_turn_oracle import ENDS_AT_TARGET
from zerg.qa.pi_family_turn_oracle import NEGATIVE_CONTROLS

pytestmark = pytest.mark.timeout(240)

# The waits a fault runs to their end, cut short here: the reply to a send that was dropped, owners that were never
# told to stop and the run they keep from being retired.
_FAULT_WAITS = {
    "send": {"_wait_native_marker": 6},
    "terminate": {
        "_wait_recorded_owners_dead": 2,
        "_wait_stopped": 2,
        "_record_retirement_claim_terminal": 2,
        "_wait_served_run_retirement": 2,
    },
}


@pytest.mark.parametrize("control", sorted(ENDS_AT_TARGET))
@pytest.mark.parametrize("provider", ["omp", "pi"])
def test_a_control_that_ends_at_its_target_decides(provider, control, tmp_path, monkeypatch, capsys):
    """The control passes, for the code it expects, and its summary lets the factory certify it.

    The factory refuses a control whose run reports any false cell but the target (control-plane
    ``negative_controls.normalize_verdict``), and a run that ends at its target has cells it never decided.
    """

    monkeypatch.delenv(fake.FAULT_ENV, raising=False)
    for name, seconds in _FAULT_WAITS[control].items():
        monkeypatch.setattr(
            helm, name, lambda *a, _wait=getattr(helm, name), _seconds=seconds, **k: _wait(*a, **{**k, "timeout": _seconds})
        )
    code, root, _left = fake.run_lifecycle(provider, tmp_path, monkeypatch.setenv, arguments=("--negative-control", control))
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    capsys.readouterr()

    _fault, target, verdict_key, expected = NEGATIVE_CONTROLS[control]
    verdict = result["negative_control"]
    assert (code, result["status"], verdict["status"]) == (0, "pass", "pass"), verdict
    assert verdict["fault_fired"] and verdict["preconditions_held"] and verdict["target_rejected"]
    assert verdict["observed_code"] == result["observation"][verdict_key]["code"] == expected
    assert [cell for cell, held in result["assertions"].items() if not held] == [f"{provider}_{target}"]
    # The failure that ended the run is kept as the control's evidence, not as the run's.
    assert result["observation"]["failure_after_control_verdict"] and "error" not in result
    # The receipt is the launcher's, for this session and this fault.
    receipts = map(json.loads, (root / "qa-fault-receipt.jsonl").read_text(encoding="utf-8").splitlines())
    assert {(line["fault"], line["session_id"]) for line in receipts} == {(verdict["fault"], result["observation"]["session_id"])}
