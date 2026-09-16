"""Oracle self-tests for codex_helm_lifecycle: recorded good and bad rollout shapes."""

from zerg.qa import codex_helm_lifecycle as lifecycle


def _started(turn):
    return {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}}


def _user(turn, text):
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "turn_id": turn,
            "item": {"type": "UserMessage", "content": [{"type": "text", "text": text}]},
        },
    }


def _call(step):
    return {"type": "response_item", "payload": {"type": "function_call", "arguments": f'{{"cmd":"sleep 4; echo {step}"}}'}}


def _complete(turn, message):
    return {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn, "last_agent_message": message}}


STEER = "LH_CODEX_STEER_x"


def _steer_obs(**overrides):
    obs = {"marker": STEER, "steered_turn_id": "t1", "send_turn_id": "t1", "steer_returncode": 0}
    obs.update(overrides)
    return obs


def test_steer_inside_active_turn_passes():
    records = [_started("t1"), _user("t1", "run steps"), _call("LH_STEER_STEP_1"), _user("t1", STEER), _complete("t1", STEER)]
    assert lifecycle.steer_active_holds(_steer_obs(), records)
    # Later scenario phases start their own turns; they do not taint the steer.
    assert lifecycle.steer_active_holds(_steer_obs(), [*records, _started("t2"), _complete("t2", "after")])


def test_queued_follow_up_turn_is_rejected():
    records = [
        _started("t1"),
        _user("t1", "run steps"),
        *(_call(f"LH_STEER_STEP_{n}") for n in range(1, 7)),
        _complete("t1", "DONE"),
        _started("t2"),
        _user("t2", STEER),
        _complete("t2", STEER),
    ]
    assert not lifecycle.steer_active_holds(_steer_obs(), records)


def test_steer_that_did_not_change_course_is_rejected():
    records = [
        _started("t1"),
        _call("LH_STEER_STEP_1"),
        _user("t1", STEER),
        *(_call(f"LH_STEER_STEP_{n}") for n in range(2, 7)),
        _complete("t1", STEER),
    ]
    assert not lifecycle.steer_active_holds(_steer_obs(), records)


def test_abort_requires_interrupted_status_and_completed_follow_up():
    records = [_started("t1"), _call("LH_ABORT_STEP_1"), _started("t2"), _user("t2", "after"), _complete("t2", "LH_AFTER")]
    good = {
        "aborted_turn_id": "t1",
        "follow_up_turn_id": "t2",
        "interrupt_returncode": 0,
        "aborted_terminal_status": "interrupted",
        "follow_up_marker": "LH_AFTER",
        "follow_up_terminal_status": "completed",
    }
    assert lifecycle.abort_native_holds(good, records)
    assert not lifecycle.abort_native_holds({**good, "aborted_terminal_status": "completed"}, records)
    no_op = [_started("t1"), *(_call(f"LH_ABORT_STEP_{n}") for n in range(1, 7)), _complete("t1", "DONE"), *records[2:]]
    assert not lifecycle.abort_native_holds(good, no_op)


def test_send_idle_rejects_fabricated_turn():
    records = [_started("t1"), _user("t1", "LH_SEND"), _complete("t1", "LH_SEND")]
    obs = {"marker": "LH_SEND", "turn_id": "t1", "quiescent_before_send": True, "final_turn_status": "completed"}
    assert lifecycle.send_idle_holds(obs, records)
    assert not lifecycle.send_idle_holds({**obs, "turn_id": "qa-fault-send-noop"}, [])


def test_terminate_requires_recorded_pids_dead():
    verified = {"verified": True, "socket_absent": True, "owned_processes_dead": True}
    assert lifecycle.terminate_owned_holds({"stop_verification": verified, "recorded_pids": [10, 11], "recorded_pids_alive": []})
    assert not lifecycle.terminate_owned_holds({"stop_verification": verified, "recorded_pids": [10], "recorded_pids_alive": [10]})
