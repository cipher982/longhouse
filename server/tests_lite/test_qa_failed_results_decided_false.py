from zerg.qa.failed_results import reached_only

MARKERS = {"cell": ("controls.send", "convergence.send")}
DECIDED = {"cell": ("target_failures.send",)}


def test_a_failed_step_with_its_decided_false_record_reports_false_without_the_later_records() -> None:
    observation = {"controls": {"send": {}}, "target_failures": {"send": {"code": "send_accepted_without_a_turn"}}}
    assert reached_only({"cell": False}, observation, MARKERS, DECIDED) == {"cell": False}


def test_without_the_decided_false_record_the_step_is_unreached() -> None:
    assert reached_only({"cell": False}, {"controls": {"send": {"x": 1}}}, MARKERS, DECIDED) == {}


def test_a_decided_false_record_never_makes_a_passing_cell_reported() -> None:
    observation = {"target_failures": {"send": {"code": "x"}}}
    assert reached_only({"cell": True}, observation, MARKERS, DECIDED) == {}
