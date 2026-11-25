"""The all-checks run: what it plans, how it cuts shards, how it judges rows and which rows it runs again. No provider runs."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from zerg.qa import terminal_truth_mutations as mutations
from zerg.qa import terminal_truth_parallel as parallel
from zerg.qa.terminal_truth_kinds import KIND_EXPECTATIONS
from zerg.qa.terminal_truth_kinds import KNOWN_LATE_STATE
from zerg.qa.terminal_truth_kinds import kinds_verdict
from zerg.qa.terminal_truth_verdicts import declared_cells
from zerg.qa.terminal_truth_verdicts import load_expectations

ENGINE, CLI = Path("/e/longhouse-engine"), Path("/e/longhouse")
FAULT_ENGINE, FAULT_CLI = Path("/e/fault/longhouse-engine"), Path("/e/fault/longhouse")


def full_plan(**overrides) -> parallel.Plan:
    options = {"scenarios": parallel.ALL_SCENARIOS, "providers": None, "modes": None, "with_mutations": True, "only": None}
    return parallel.make_plan(**{**options, **overrides})


def shards_of(plan: parallel.Plan, providers_per_shard: int = 1) -> list[parallel.Shard]:
    copies = {m.name: Path(f"/c/{m.name}/server") for m in plan.selected if m.side == "server"}
    return parallel.make_shards(
        plan, engine=ENGINE, cli=CLI, providers_per_shard=providers_per_shard, tokens=100, rate=20.0, fault_engine=FAULT_ENGINE,
        fault_cli=FAULT_CLI, server_copies=copies,
    )  # fmt: skip


# ----- the plan


def test_the_plan_runs_every_declared_reply_cell_twice_and_every_cell_with_a_published_kind_expectation_once() -> None:
    plan = full_plan(with_mutations=False)
    declared = [spec.key for spec in declared_cells(load_expectations())]
    want = {f"{scenario}:{cell}" for scenario in parallel.REPLY_SCENARIOS for cell in declared}
    want |= {f"{scenario}:{cell}" for (cell, scenario) in KIND_EXPECTATIONS}
    assert set(plan.units) == want
    assert not plan.selected and not plan.mutated and not plan.extras


def test_every_mutation_has_an_unmutated_half_that_is_in_the_plan_and_a_mutated_half_on_the_same_cell_and_options() -> None:
    plan = full_plan()
    assert [m.name for m in plan.selected] == [m.name for m in mutations.MUTATIONS]
    assert set(plan.extras) == set(parallel.EXTRA_CHECKS)
    for m in mutations.MUTATIONS:
        base, mutated = plan.units[plan.baseline_of[m.name]], plan.mutated[m.name]
        assert (
            (base.cell, base.scenario, base.options)
            == (mutated.cell, mutated.scenario, mutated.options)
            == (m.cell, m.scenario or "long_reply", base.options)
        )
        assert mutated.mutation == m.name and base.mutation is None
        if m.scenario and m.cell.endswith("/helm"):
            assert base.options == parallel.Options(m.warm, m.text_before_tool, m.engine_delay_ms)


def test_a_mutation_whose_options_are_the_gates_reuses_the_gates_run_of_that_cell() -> None:
    plan = full_plan()
    standard = set(full_plan(with_mutations=False).units)
    reused = [m.name for m in mutations.MUTATIONS if plan.baseline_of[m.name] in standard]
    # Most mutations cost one cell run, not two. The exact count moves when a mutation is added with unusual options.
    assert len(reused) >= len(mutations.MUTATIONS) - 4, sorted(set(plan.baseline_of.values()) - standard)
    assert len(plan.units) - len(standard) <= 4


def test_narrowing_to_one_mutation_keeps_only_its_two_runs_among_the_mutation_checks() -> None:
    plan = full_plan(scenarios=["long_reply"], providers=["codex"], only=["claude-helm-thinking-forward"])
    assert list(plan.mutated) == ["claude-helm-thinking-forward"] and not plan.extras
    # The narrowed-out cell is still run unmutated: a mutation with no unmutated half proves nothing.
    assert plan.baseline_of["claude-helm-thinking-forward"] in plan.units
    assert full_plan(only=["half-speed"]).extras.keys() == {"half-speed"}


# ----- shards


def test_every_planned_run_is_in_exactly_one_shard() -> None:
    plan = full_plan()
    for providers_per_shard in (1, 2, 7):
        shards = shards_of(plan, providers_per_shard)
        ids = [u.id for s in shards for u in s.units]
        assert sorted(ids) == sorted([*plan.units, *(u.id for u in plan.mutated.values()), *(u.id for u in plan.extras.values())])
        assert len({s.name for s in shards}) == len(shards)  # the name is the log and result file


def test_one_provider_per_shard_never_puts_two_providers_on_one_host_and_omp_never_shares_with_pi() -> None:
    plan = full_plan(with_mutations=False)
    for shard in shards_of(plan, 1):
        assert len({u.cell.split("/")[0] for u in shard.units}) == 1, shard.name
    for shard in shards_of(plan, 2):
        assert not {"omp", "pi"} <= {u.cell.split("/")[0] for u in shard.units}, shard.name


def test_a_mutated_run_gets_its_own_shard_with_the_qa_engine_switched_or_the_patched_server() -> None:
    plan = full_plan()
    by_unit = {s.units[0].id: s for s in shards_of(plan) if len(s.units) == 1}
    for m in mutations.MUTATIONS:
        shard = by_unit[f"mutated:{m.name}"]
        if m.side == "server":
            assert (shard.engine, shard.cli) == (ENGINE, CLI)
            assert shard.flags[-2:] == ["--host-env", f"PYTHONPATH=/c/{m.name}/server"]
        else:
            assert (shard.engine, shard.cli) == (FAULT_ENGINE, FAULT_CLI)  # one QA engine for every engine defect
            assert "--host-env" not in shard.flags
            switched = [shard.flags[i + 1] for i, flag in enumerate(shard.flags) if flag == "--engine-env"]
            assert f"LONGHOUSE_QA_MUTANT={m.engine_switch}" in switched
        base_shard = next(s for s in shards_of(plan) if plan.baseline_of[m.name] in [u.id for u in s.units])
        assert (base_shard.engine, base_shard.cli) == (ENGINE, CLI)


def test_the_two_checks_with_no_edit_run_codex_helm_with_a_reworded_ready_text_and_at_half_the_rate() -> None:
    by_name = {s.units[0].id: s for s in shards_of(full_plan())}
    assert by_name["extra:half-speed"].flags == ["--tokens", "100", "--rate", "10.0"]
    reworded = by_name["extra:reworded-ready-string"].flags
    assert reworded[:4] == ["--tokens", "100", "--rate", "20.0"] and "--ready-text" in reworded


def test_a_shard_is_the_serial_command_on_named_cells(tmp_path) -> None:
    runner = parallel.Runner(tmp_path / "out", tmp_path / "scratch", lambda _m: None)
    shard = next(s for s in shards_of(full_plan(with_mutations=False)) if s.name == "thinking-0")
    argv = runner.argv(shard, tmp_path / "x.json")
    assert argv[1:6] == ["-m", "zerg.qa.terminal_truth", "run", "--scenario", "thinking"]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--cell"] == [u.cell for u in shard.units]
    assert "--keep" in argv and "--warm-session" in argv


def test_the_serial_command_refuses_a_cell_that_is_not_declared(capsys, tmp_path) -> None:
    from zerg.qa import terminal_truth as tt

    binary = tmp_path / "longhouse-engine"
    binary.write_text("")
    with pytest.raises(SystemExit):
        tt.main(["run", "--cell", "nosuch/helm", "--engine", str(binary), "--longhouse-cli", str(binary)])
    assert "nosuch" in capsys.readouterr().err


# ----- verdicts


def reply(cell: str, outcome: str = "pass", stage: str | None = None, reason: str = "") -> dict:
    provider, mode = cell.split("/")
    return {"cell": cell, "provider": provider, "mode": mode, "status": "measured", "outcome": outcome, "first_failed_stage": stage,
            "reason": reason, "stages": {}, "lanes": [{"lane": "workspace stream (SSE)", "median_delay_s": 0.1}]}  # fmt: skip


def fake_rows(plan: parallel.Plan, verdicts: dict[str, tuple[str, str | None, str]], monkeypatch) -> dict[str, dict]:
    """One row per planned run; ``verdicts`` overrides what a run came out as (default: as its check wants)."""

    rows: dict[str, dict] = {}
    by_name = {m.name: m for m in plan.selected}
    table: dict[str, tuple[str, str | None, str]] = {}
    for unit in parallel._all_units(plan).values():
        if unit.id in verdicts:
            table[unit.id] = verdicts[unit.id]
        elif unit.mutation:
            table[unit.id] = ("fail", by_name[unit.mutation].stages[0], "the defect")
        elif unit.extra == "reworded-ready-string":
            table[unit.id] = ("fail", "preflight", "never ready")
        else:
            table[unit.id] = ("pass", None, "")
        rows[unit.id] = {"id": unit.id}
    monkeypatch.setattr(parallel, "verdict_of", lambda unit, row: table[row["id"]])
    return rows


def test_all_as_wanted_is_a_pass_with_nothing_to_run_again(monkeypatch) -> None:
    plan = full_plan()
    judged = parallel.judge(plan, fake_rows(plan, {}, monkeypatch))
    assert judged.ok and judged.wrong == []
    assert len(judged.cells) == len(plan.units) and len(judged.mutation_rows) == len(mutations.MUTATIONS) + 2
    assert all(row["ok"] for row in judged.mutation_rows)


def test_a_failing_unmutated_cell_fails_the_run_and_every_mutation_that_reads_it(monkeypatch) -> None:
    plan = full_plan()
    victim = next(m for m in mutations.MUTATIONS if m.scenario == "thinking" and m.cell == "claude/helm")
    base = plan.baseline_of[victim.name]
    judged = parallel.judge(plan, fake_rows(plan, {base: ("fail", "preflight", "the provider never became ready")}, monkeypatch))
    assert not judged.ok
    assert judged.cells[base]["outcome"] == "fail" and judged.wrong == [base]  # once, though several mutations read it
    readers = [m.name for m in mutations.MUTATIONS if plan.baseline_of[m.name] == base]
    not_ok = [row["check"] for row in judged.mutation_rows if not row["ok"]]
    assert not_ok == [f"mutation {name}" for name in readers] and len(readers) >= 2


def test_a_mutated_engine_that_the_gate_passes_or_fails_at_another_stage_is_a_miss_and_is_run_again(monkeypatch) -> None:
    plan = full_plan()
    m = mutations.MUTATIONS[0]
    for verdict in (("pass", None, ""), ("fail", "not-a-stage", "elsewhere")):
        judged = parallel.judge(plan, fake_rows(plan, {f"mutated:{m.name}": verdict}, monkeypatch))
        assert not judged.ok and judged.wrong == [f"mutated:{m.name}"]
        (row,) = [r for r in judged.mutation_rows if not r["ok"]]
        assert row["check"] == f"mutation {m.name}" and row["baseline"] == "pass"


def test_a_late_state_is_a_finding_only_on_a_cell_where_it_is_declared_known(monkeypatch) -> None:
    plan = full_plan(with_mutations=False)
    (known,) = KNOWN_LATE_STATE
    late = ("fail", "state_shown", "the host served a state and woke nobody")
    judged = parallel.judge(plan, fake_rows(plan, {f"thinking:{known}": late, "thinking:claude/helm": late}, monkeypatch))
    assert judged.cells[f"thinking:{known}"]["outcome"] == "finding"
    assert judged.cells["thinking:claude/helm"]["outcome"] == "fail"
    assert judged.wrong == ["thinking:claude/helm"] and not judged.ok
    only_known = parallel.judge(plan, fake_rows(plan, {f"thinking:{known}": late}, monkeypatch))
    assert only_known.ok and only_known.wrong == []


def test_a_known_bad_reply_cell_is_good_and_a_live_kinds_cell_that_was_only_measured_is_not() -> None:
    reply_unit = parallel.Unit("long_reply:codex/helm", "codex/helm", "long_reply")
    assert parallel.gate_outcome(reply_unit, ("known_bad", "engine_posted", "declared")) == "pass"
    assert parallel.gate_outcome(reply_unit, ("fail", "engine_posted", "")) == "fail"
    kinds_unit = parallel.Unit("tool:codex/helm", "codex/helm", "tool", parallel.Options(warm=True))
    assert parallel.gate_outcome(kinds_unit, ("measured", None, "no expectation")) == "fail"
    assert parallel.gate_outcome(kinds_unit, ("not_measured", None, "")) == "fail"


def test_the_kinds_verdict_names_preflight_when_the_scenario_did_not_happen() -> None:
    result = {"cell": "claude/helm", "scenario": "thinking", "outcome": "invalid", "reason": "the model served nothing",
              "checks": [{"name": "model_served_every_turn", "ok": False}]}  # fmt: skip
    assert kinds_verdict(result) == ("fail", "preflight", "the model served nothing")
    assert kinds_verdict({"outcome": "not_measured", "reason": "no subagent"}) == ("not_measured", None, "no subagent")


# ----- the run: reruns, files, exit


class FakeRunner:
    """Stands in for the child processes: a scripted verdict per unit id and attempt."""

    def __init__(self, script: dict[str, list[tuple[str, str | None, str]]], scratch: Path) -> None:
        self.script, self.scratch, self.calls, self.epoch = script, scratch, [], 0.0
        self.hold = 0.0  # seconds a shard takes, so shards can be made to overlap
        self.hosts: parallel.HostPool | None = None
        self.stopped = parallel.threading.Event()

    def run(self, shard: parallel.Shard, attempt: str = "") -> parallel.ShardRun:
        self.calls.append((shard.name, attempt, [u.id for u in shard.units]))
        time.sleep(self.hold)
        rows = {}
        for unit in shard.units:
            queue = self.script.get(unit.id) or []
            rows[unit.id] = {"id": unit.id, "verdict": queue.pop(0) if queue else None, "cell": unit.cell, "lanes": []}
        scratch = self.scratch / f"{shard.name}{attempt}"
        scratch.mkdir(parents=True, exist_ok=True)
        run = parallel.ShardRun(shard, rows, 0, 1.0, 0.0, scratch, scratch.with_suffix(".log"))
        run.embedding_loaded = True if shard.embedding else None
        return run

    def kill_all(self) -> None:
        pass


def run_with(monkeypatch, tmp_path, script, *extra: str) -> tuple[int, dict, FakeRunner]:
    plan_names = {m.name: m for m in mutations.MUTATIONS}

    def verdict_of(unit: parallel.Unit, row: dict) -> tuple[str, str | None, str]:
        if row.get("verdict"):
            return tuple(row["verdict"])
        if unit.mutation:
            return ("fail", plan_names[unit.mutation].stages[0], "the defect")
        if unit.extra == "reworded-ready-string":
            return ("fail", "preflight", "never ready")
        return ("pass", None, "")

    fake = FakeRunner(script, tmp_path / "scratch")
    monkeypatch.setattr(parallel, "Runner", lambda *a, **k: fake)
    monkeypatch.setattr(parallel, "verdict_of", verdict_of)
    monkeypatch.setattr(parallel, "SCRATCH_ROOT", tmp_path / "scratch")
    monkeypatch.setattr(parallel, "memory_limit", lambda: None)
    monkeypatch.setattr(parallel, "short_scratch", lambda root: (root, None))
    monkeypatch.setattr(mutations, "prepare_server_copy", lambda m, copy: copy / "server")
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = tmp_path / "longhouse-engine"
    engine.write_text("")
    out = tmp_path / "out"
    code = parallel.main(["--engine", str(engine), "--longhouse-cli", str(engine), "--out-dir", str(out), "--jobs", "4", *extra])
    return code, json.loads((out / "fleet.json").read_text()), fake


def test_a_clean_run_exits_zero_writes_every_file_and_keeps_no_scratch(monkeypatch, tmp_path) -> None:
    code, fleet, fake = run_with(monkeypatch, tmp_path, {})
    out = tmp_path / "out"
    assert code == 0 and fleet["state"] == "success" and fleet["reruns"] == [] and fleet["kept_scratch"] == []
    assert not any((tmp_path / "scratch").iterdir())
    plan = full_plan()
    assert fleet["planned"]["cells"] == sorted(plan.units)
    assert fleet["planned"]["mutations"] == [m.name for m in mutations.MUTATIONS] + list(parallel.EXTRA_CHECKS)
    for scenario in parallel.REPLY_SCENARIOS:
        rows = json.loads((out / f"pass-1-{scenario}.json").read_text())
        assert len(rows) == len(declared_cells(load_expectations()))
    assert len(json.loads((out / "kinds.json").read_text())) >= len(KIND_EXPECTATIONS)
    table = json.loads((out / "mutations.json").read_text())
    assert len(table) == len(mutations.MUTATIONS) + 2 and all(row["ok"] for row in table)
    assert all(attempt == "" for _name, attempt, _ids in fake.calls)
    assert fleet["embedding_lane"] == {"shard": "long_reply-0", "loaded": True}
    assert fleet["jobs"] == fleet["jobs_asked"] == 4


def test_a_row_that_came_out_wrong_is_run_again_alone_and_the_second_result_decides(monkeypatch, tmp_path) -> None:
    slow = ("fail", "stream_delivered", "median 1.7 s, budget 1.0 s")
    code, fleet, fake = run_with(monkeypatch, tmp_path, {"long_reply:claude/console": [slow, ("pass", None, "")]})
    assert code == 0 and fleet["state"] == "success"
    assert fleet["reruns"] == [
        {"id": "long_reply:claude/console", "first": "fail at stream_delivered: median 1.7 s, budget 1.0 s", "again": "pass"}
    ]
    again = [(name, ids) for name, attempt, ids in fake.calls if attempt == ".again"]
    assert again == [("long_reply-claude-console", ["long_reply:claude/console"])]
    rows = json.loads((tmp_path / "out" / "pass-1-long_reply.json").read_text())
    assert next(r for r in rows if r["cell"] == "claude/console")["first_try"].startswith("fail at stream_delivered")


def test_a_row_that_is_wrong_both_times_fails_the_run_and_keeps_its_scratch(monkeypatch, tmp_path) -> None:
    lost = ("fail", "host_published", "the host dropped it")
    code, fleet, _fake = run_with(monkeypatch, tmp_path, {"thinking:claude/helm": [lost, lost]})
    assert code == 1 and fleet["state"] == "failure"
    assert [c["id"] for c in fleet["cells"] if c["outcome"] == "fail"] == ["thinking:claude/helm"]
    assert len(fleet["kept_scratch"]) == 2 and all(Path(p).is_dir() for p in fleet["kept_scratch"])


def test_no_rerun_judges_the_first_try_and_too_many_wrong_rows_are_not_run_again(monkeypatch, tmp_path) -> None:
    slow = ("fail", "stream_delivered", "slow")
    code, fleet, fake = run_with(monkeypatch, tmp_path / "a", {"long_reply:claude/console": [slow]}, "--no-rerun")
    assert code == 1 and fleet["reruns"] == [] and all(attempt == "" for _n, attempt, _i in fake.calls)
    many = {uid: [slow] for uid in list(full_plan(with_mutations=False).units)[: parallel.MAX_RERUNS + 1]}
    code, fleet, fake = run_with(monkeypatch, tmp_path / "b", many)
    assert code == 1 and fleet["reruns"] == [] and all(attempt == "" for _n, attempt, _i in fake.calls)


def test_a_shard_that_left_no_result_for_a_cell_is_a_preflight_failure_with_the_end_of_its_log(tmp_path) -> None:
    runner = parallel.Runner(tmp_path / "out", tmp_path / "scratch", lambda _m: None)
    shard = parallel.Shard("x", "long_reply", [parallel.Unit("long_reply:codex/helm", "codex/helm", "long_reply")], ENGINE, CLI)
    log = tmp_path / "x.log"
    log.write_text("Traceback...\nRuntimeError: port in use\n")
    rows = runner._rows(shard, tmp_path / "missing.json", 2, "", log)
    row = rows["long_reply:codex/helm"]
    assert parallel.verdict_of(shard.units[0], row)[:2] == ("fail", "preflight")
    assert "exit 2" in row["reason"] and "port in use" in row["reason"]


def test_a_stopped_run_starts_nothing_more_and_its_unrun_cells_are_failures(tmp_path) -> None:
    runner = parallel.Runner(tmp_path / "out", tmp_path / "scratch", lambda _m: None)
    runner.stop()
    shard = parallel.Shard("x", "long_reply", [parallel.Unit("long_reply:codex/helm", "codex/helm", "long_reply")], ENGINE, CLI)
    run = runner.run(shard)
    assert run.exit is None and not run.scratch.exists()
    outcome, stage, reason = parallel.verdict_of(shard.units[0], run.rows["long_reply:codex/helm"])
    assert (outcome, stage) == ("fail", "preflight") and "the run was stopped before this shard started" in reason


# ----- memory: the embedding lane and how many shards at once


def test_exactly_one_shard_runs_the_embedding_lane_and_it_is_the_unmutated_long_reply_shard_with_claude(tmp_path) -> None:
    plan = full_plan()
    for per_shard in (1, 2):
        shards = shards_of(plan, per_shard)
        with_lane = [s for s in shards if s.embedding]
        assert len(with_lane) == 1
        (shard,) = with_lane
        assert shard.scenario == "long_reply" and all(u.mutation is None and u.extra is None for u in shard.units)
        assert any(u.cell.startswith("claude/") for u in shard.units)
        at = shard.flags.index(parallel.EMBEDDING_ON)
        assert shard.flags[at - 1] == "--host-env"
        assert not any(parallel.EMBEDDING_ON in s.flags for s in shards if s is not shard)
    copies = {m.name: tmp_path / m.name for m in plan.selected if m.side == "server"}
    cached = parallel.make_shards(
        plan, engine=ENGINE, cli=CLI, providers_per_shard=2, tokens=100, rate=20.0, fault_engine=FAULT_ENGINE, fault_cli=FAULT_CLI, server_copies=copies,
        embed_model_dir=tmp_path / "model",
    )  # fmt: skip
    (shard,) = [s for s in cached if s.embedding]
    assert f"LONGHOUSE_EMBED_MODEL_DIR={tmp_path / 'model'}" in shard.flags
    without_claude = shards_of(full_plan(providers=["codex"], with_mutations=False))
    assert not any(s.embedding for s in without_claude)


def test_a_scratch_host_starts_with_the_embedding_lane_off_and_a_host_env_turns_it_on(monkeypatch, tmp_path) -> None:
    from zerg.qa import terminal_truth_providers as providers

    seen: list[dict[str, str]] = []

    def fake_popen(_argv, **kwargs):
        seen.append(kwargs["env"])
        raise RuntimeError("stop here")

    monkeypatch.setattr(providers.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("LONGHOUSE_EMBEDDING_PROJECTOR_DISABLED", "0")  # what the caller's shell carries is not what decides
    for extra in (None, {"LONGHOUSE_EMBEDDING_PROJECTOR_DISABLED": "0"}):
        with pytest.raises(RuntimeError, match="stop here"):
            providers.ScratchRuntimeHost(tmp_path, extra).start()
    assert [env["LONGHOUSE_EMBEDDING_PROJECTOR_DISABLED"] for env in seen] == ["1", "0"]


def test_the_server_leaves_the_embedding_projector_off_only_when_the_switch_is_set(monkeypatch) -> None:
    from types import SimpleNamespace

    from zerg import lifespan
    from zerg.services import embeddings_v2_projector

    started: list[int] = []
    monkeypatch.setattr(embeddings_v2_projector, "start_embeddings_v2_projector", lambda: started.append(1) or True)
    for value, want_started in (("1", False), ("true", False), ("0", True), ("", True)):
        started.clear()
        monkeypatch.setenv("LONGHOUSE_EMBEDDING_PROJECTOR_DISABLED", value)
        app = SimpleNamespace(state=SimpleNamespace())
        lifespan._start_local_embedding_projector(app)
        assert (app.state.embeddings_v2_projector_started, bool(started)) == (want_started, want_started), value


def test_the_run_reads_whether_the_model_loaded_from_the_host_log_of_the_shard(tmp_path) -> None:
    (tmp_path / "ab12").mkdir()
    (tmp_path / "ab12" / "runtime-host.log").write_text("INFO startup\n")
    assert not parallel._host_log_has(tmp_path, parallel.EMBEDDING_LOADED)
    (tmp_path / "cd34").mkdir()
    (tmp_path / "cd34" / "runtime-host.log").write_text("INFO [ZERG.SERVICES.LOCAL_EMBEDDER] Local embedder loaded model=/m threads=4\n")
    assert parallel._host_log_has(tmp_path, parallel.EMBEDDING_LOADED)


def test_the_log_line_the_run_looks_for_is_the_one_the_embedder_writes() -> None:
    source = (parallel.SERVER_ROOT / "zerg" / "services" / "local_embedder.py").read_text()
    assert f'logger.info("{parallel.EMBEDDING_LOADED} ' in source


GIB = 2**30


def gate_with(monkeypatch, limit_gib: float | None, in_use_gib: float) -> tuple[parallel.MemoryGate, list[float]]:
    """A gate that is read by hand (its thread is never started), and the reading it will see next."""

    reading = [in_use_gib * GIB]
    monkeypatch.setattr(parallel, "_memory_in_use", lambda: int(reading[0]))
    limit = None if limit_gib is None else (int(limit_gib * GIB), f"a limit of {limit_gib:.1f} GiB")
    return parallel.MemoryGate(limit), reading


def test_memory_is_read_from_the_cgroup_whose_limit_the_run_meets_first(tmp_path) -> None:
    root = tmp_path / "cgroup"
    scope = root / "user.slice" / "app.slice" / "run-1.scope"
    scope.mkdir(parents=True)
    own = tmp_path / "self"
    own.write_text("0::/user.slice/app.slice/run-1.scope\n")
    (root / "memory.max").write_text("25769803776\n")
    (root / "user.slice" / "memory.max").write_text("max\n")
    (scope / "memory.max").write_text("max\n")
    assert parallel.limiting_cgroup(root, own) == root  # only the container is limited
    (scope / "memory.max").write_text("8589934592\n")
    assert parallel.limiting_cgroup(root, own) == scope  # a scope with less than the container
    (root / "user.slice" / "memory.max").write_text("4294967296\n")
    assert parallel.limiting_cgroup(root, own) == root / "user.slice"
    own.write_text("0::/\n")
    assert parallel.limiting_cgroup(root, own) == root  # a pod: the process sees its container as the root
    assert parallel.limiting_cgroup(root, tmp_path / "absent") == root


def test_a_shard_starts_only_when_the_memory_limit_has_room_for_one_more_and_one_always_runs(monkeypatch) -> None:
    gate, reading = gate_with(monkeypatch, 8.0, 0.4)
    for _ in range(4):
        assert gate._room()
        gate.enter()
    assert not gate._room()  # 0.4 in use, four shards at 1.5, one more at 1.5 and 1.5 left free is over 8
    gate.leave()
    assert gate._room()
    gate.enter()
    assert gate.widest == 4

    nothing_free, reading = gate_with(monkeypatch, 8.0, 7.9)
    assert nothing_free._room()  # nothing is running: one shard runs whatever the reading says
    nothing_free.enter()
    assert not nothing_free._room()

    unlimited, _ = gate_with(monkeypatch, None, 0.4)
    for _ in range(40):
        unlimited.enter()
    assert unlimited._room()


def test_what_a_shard_holds_is_read_from_the_run_once_a_whole_wave_has_finished_and_only_ever_raised(monkeypatch) -> None:
    lines: list[str] = []
    gate, reading = gate_with(monkeypatch, 8.0, 0.4)
    gate._log = lines.append
    for _ in range(4):
        gate.enter()
    reading[0] = 4.4 * GIB  # four shards hold 4.0 GiB between them
    gate.read()
    assert not gate.measured and not gate._room()  # none has finished: a shard that is still starting says nothing yet
    for _ in range(3):
        gate.leave()
        gate.enter()
    gate.read()
    assert not gate.measured
    gate.leave()
    gate.enter()
    gate.read()
    assert gate.measured and gate.per_shard == 1.0 * GIB
    assert lines == ["memory: a shard holds 1.00 GiB here, so a limit of 8.0 GiB holds 6 at a time"]

    assert gate._room()  # 0.4 + 4 x 1.0 + 1.0 + 1.5 is under 8
    gate.enter()
    assert not gate._room()  # one start a reading
    gate.read()
    assert gate._room()
    gate.enter()
    gate.read()
    assert not gate._room() and gate.widest == 6  # a seventh would leave less than 1.5 free

    reading[0] = 7.6 * GIB  # six shards now hold 7.2 GiB: more each than was measured
    gate.read()
    assert gate.per_shard == pytest.approx(1.2 * GIB)
    gate.leave()
    gate.leave()
    reading[0] = 5.2 * GIB
    gate.read()
    assert gate.per_shard == pytest.approx(1.2 * GIB) and gate._room()  # the figure stays: 0.4 + 4 x 1.2 + 1.2 + 1.5 is under 8
    gate.enter()
    gate.read()
    assert not gate._room()  # five at a time now, not six
    assert gate.finish() == {"before_gib": 0.4, "peak_gib": 7.6, "per_shard_gib": 1.2, "limit_gib": 8.0}


def test_a_start_is_held_by_what_is_in_use_now_when_that_is_more_than_the_shards_were_taken_to_need(monkeypatch) -> None:
    gate, reading = gate_with(monkeypatch, 16.0, 0.4)
    gate.enter()
    assert gate._room()
    reading[0] = 13.2 * GIB  # something else in this limit took the memory
    assert not gate._room()


def test_a_waiting_shard_starts_when_another_finishes_and_when_the_run_is_stopped(monkeypatch) -> None:
    gate, _ = gate_with(monkeypatch, 5.0, 0.0)
    gate.interval = 0.01
    gate.enter()
    gate.enter()
    started = threading.Event()
    waiter = threading.Thread(target=lambda: (gate.enter(), started.set()), daemon=True)
    waiter.start()
    assert not started.wait(0.1)
    gate.leave()
    assert started.wait(2.0) and gate.waited == 1 and gate.widest == 2

    stopped, _ = gate_with(monkeypatch, 5.0, 0.0)
    stopped.interval = 0.01
    stopped.enter()
    stopped.enter()
    stopped._stopped.set()
    stopped.enter()  # returns: the shard is started and fails as a stopped run's shards do
    assert stopped.running == 3


def test_a_run_that_was_given_less_memory_says_so_and_records_both_numbers(monkeypatch, tmp_path, capsys) -> None:
    code, fleet, fake = run_with(monkeypatch, tmp_path, {})
    assert (fleet["jobs"], fleet["jobs_asked"]) == (4, 4)
    fake.hold = 0.05
    monkeypatch.setattr(parallel, "memory_limit", lambda: (5 * GIB, "a limit of 5.0 GiB"))
    monkeypatch.setattr(parallel, "_memory_in_use", lambda: 0)
    out = tmp_path / "second"
    engine = tmp_path / "longhouse-engine"
    code = parallel.main(["--engine", str(engine), "--longhouse-cli", str(engine), "--out-dir", str(out), "--jobs", "8", "--no-mutations"])
    fleet = json.loads((out / "fleet.json").read_text())
    assert code == 0 and (fleet["jobs"], fleet["jobs_asked"]) == (2, 8)
    assert "2 shards at a time, not 8: a limit of 5.0 GiB" in capsys.readouterr().err


# ----- what the engine is built from


def test_everything_the_engine_embeds_at_build_time_is_inside_the_directories_the_cache_key_hashes() -> None:
    """The key hashes engine/, config/ and schemas/ at HEAD. An include that reaches elsewhere would let a stale binary through."""

    root = mutations.REPO_ROOT
    listed = subprocess.run(
        ["git", "ls-files", "engine/*.rs", "engine/**/*.rs"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.split()
    include = re.compile(r'include_(?:str|bytes)!\(\s*(?:concat!\(\s*env!\("CARGO_MANIFEST_DIR"\)\s*,\s*)?"([^"]+)"')
    outside: list[str] = []
    seen = 0
    for name in listed:
        source = root / name
        for target in include.findall(source.read_text(errors="replace")):
            seen += 1
            base = root / "engine" if target.startswith("/") else source.parent
            resolved = (base / target.lstrip("/")).resolve()
            if not any(resolved.is_relative_to(root / d) for d in mutations.ENGINE_INPUT_DIRS):
                outside.append(f"{name}: {target}")
    assert seen > 5  # the pattern still finds the includes
    assert outside == []


def test_the_summary_line_is_what_ci_greps(monkeypatch, tmp_path, capsys) -> None:
    code, _fleet, _fake = run_with(monkeypatch, tmp_path, {})
    out = capsys.readouterr().out
    assert code == 0 and "terminal truth, all checks: PASS" in out and "### Mutation tests" in out


def test_options_name_only_what_differs_from_how_the_gate_runs_the_cell() -> None:
    assert parallel.unit_id("thinking", "claude/helm", parallel.Options(warm=True)) == "thinking:claude/helm"
    assert parallel.unit_id("thinking", "claude/console", parallel.Options()) == "thinking:claude/console"
    assert (
        parallel.unit_id("tool", "antigravity/helm", parallel.Options(True, True, 150)) == "tool:antigravity/helm+text-before-tool+delay150"
    )
    assert parallel.unit_id("thinking", "claude/helm", parallel.Options()) == "thinking:claude/helm+cold"


# ----- the engine the run measures


def test_the_engine_and_the_qa_engine_are_built_once_per_set_of_inputs_and_copied_from_the_cache_after(monkeypatch, tmp_path) -> None:
    from zerg.qa import terminal_truth_engine as engine

    builds: list[str] = []
    targets: list[Path | None] = []

    def fake_build(flags: str = engine.FLAGS, target: Path | None = None) -> dict[str, Path]:
        builds.append(flags)
        targets.append(target)
        made = tmp_path / "target"
        made.mkdir(exist_ok=True)
        for name in mutations.BINARIES:
            (made / name).write_text(f"{name} build {len(builds)}")
            (made / name).chmod(0o755)
        return {name: made / name for name in mutations.BINARIES}

    digest = ["a" * 64]
    monkeypatch.setattr(engine, "build", fake_build)
    monkeypatch.setattr(mutations, "engine_inputs_digest", lambda **_k: digest[0])
    cache = tmp_path / "cache"

    assert engine.provide(tmp_path / "one", cache) == ("miss", "built")
    assert builds == [engine.FLAGS, engine.FAULT_FLAGS]  # the engine, then the same engine with the fault seams
    assert "qa-fault-injection" in engine.FAULT_FLAGS and "qa-fault-injection" not in engine.FLAGS
    assert targets == [engine.cache_target(cache)] * 2  # the dependencies stay compiled between runs
    assert (tmp_path / "one" / "longhouse-engine").read_text() == "longhouse-engine build 1"
    assert (tmp_path / "one" / "fault" / "longhouse-engine").read_text() == "longhouse-engine build 2"
    assert engine.provide(tmp_path / "two", cache) == ("hit", "cached") and len(builds) == 2
    assert (tmp_path / "two" / "fault" / "longhouse").read_text() == "longhouse build 2"
    assert engine.provide(tmp_path / "three", None) == ("off", "built") and len(builds) == 4
    assert targets[2:] == [None, None]  # no cache, the checkout's own target

    digest[0] = "b" * 64  # the engine sources changed
    assert engine.provide(tmp_path / "four", cache) == ("miss", "built") and len(builds) == 6
    assert [entry.name for entry in cache.iterdir() if not entry.name.startswith("cargo-target-")] == ["b" * 24]  # one build is kept


def test_a_run_without_mutation_checks_builds_no_qa_engine_and_leaves_the_engine_for_the_next_run(monkeypatch, tmp_path) -> None:
    from zerg.qa import terminal_truth_engine as engine

    builds: list[str] = []

    def fake_build(flags: str = engine.FLAGS, target: Path | None = None) -> dict[str, Path]:
        builds.append(flags)
        made = tmp_path / f"target{len(builds)}"
        made.mkdir()
        for name in mutations.BINARIES:
            (made / name).write_text(f"{name} {'qa' if 'qa-fault-injection' in flags else 'measured'} build {len(builds)}")
            (made / name).chmod(0o755)
        return {name: made / name for name in mutations.BINARIES}

    monkeypatch.setattr(engine, "build", fake_build)
    monkeypatch.setattr(mutations, "engine_inputs_digest", lambda **_k: "c" * 64)
    cache = tmp_path / "cache"

    assert engine.provide(tmp_path / "cells", cache, fault=False) == ("miss", "skipped")
    assert builds == [engine.FLAGS] and not (tmp_path / "cells" / "fault").exists()
    assert engine.provide(tmp_path / "cells-again", cache, fault=False) == ("hit", "skipped") and len(builds) == 1
    # A run with mutation checks on the same inputs builds only the QA engine, and keeps it beside the measured one.
    assert engine.provide(tmp_path / "mutations", cache) == ("miss", "built") and builds == [engine.FLAGS, engine.FAULT_FLAGS]
    assert (tmp_path / "mutations" / "longhouse-engine").read_text() == "longhouse-engine measured build 1"
    assert (tmp_path / "mutations" / "fault" / "longhouse-engine").read_text() == "longhouse-engine qa build 2"
    assert engine.provide(tmp_path / "mutations-again", cache) == ("hit", "cached") and len(builds) == 2


def test_the_engine_lines_name_the_measured_engine_as_the_qa_engine_when_none_is_built(monkeypatch, tmp_path, capsys) -> None:
    from zerg.qa import terminal_truth_engine as engine

    def fake_provide(out: Path, cache: Path | None, *, fault: bool = True) -> tuple[str, str]:
        for directory in [out, *([out / engine.FAULT_DIR] if fault else [])]:
            directory.mkdir(parents=True, exist_ok=True)
            for name in mutations.BINARIES:
                (directory / name).write_text(name)
                (directory / name).chmod(0o755)
        return "off", "built" if fault else "skipped"

    monkeypatch.setattr(engine, "provide", fake_provide)
    monkeypatch.setenv("TT_MUTATIONS", "0")  # the CI action's decision: this run has no mutation checks
    assert engine.main(["--out", str(tmp_path / "a")]) == 0
    lines = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert lines["fault_engine"] == lines["engine"] and lines["fault_cli"] == lines["cli"] and lines["fault"] == "skipped"
    monkeypatch.setenv("TT_MUTATIONS", "1")
    assert engine.main(["--out", str(tmp_path / "b")]) == 0
    lines = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert lines["fault_engine"] == str(tmp_path / "b" / "fault" / "longhouse-engine") and lines["fault"] == "built"


def test_a_cached_cargo_target_over_its_bound_is_started_again(monkeypatch, tmp_path) -> None:
    from zerg.qa import terminal_truth_engine as engine

    target = tmp_path / "cargo-target-x"
    (target / "ci").mkdir(parents=True)
    (target / "ci" / "big").write_bytes(b"x" * 2048)
    monkeypatch.setattr(engine, "TARGET_KEEP_BYTES", 4096)
    assert engine._bounded(target) == target and target.exists()
    (target / "ci" / "bigger").write_bytes(b"x" * 4096)
    assert engine._bounded(target) == target and not target.exists()


def test_the_engine_and_the_qa_engine_are_keyed_by_different_flags_over_the_same_inputs(monkeypatch) -> None:
    from zerg.qa import terminal_truth_engine as engine

    seen: list[list[str]] = []

    def fake_run(command, **_kwargs):
        seen.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="tree-or-rustc\n", stderr="")

    monkeypatch.setattr(mutations.subprocess, "run", fake_run)
    qa, measured = mutations.engine_inputs_digest(), mutations.engine_inputs_digest(flags=engine.FLAGS)
    assert qa != measured
    listing = next(command for command in seen if command[:2] == ["git", "ls-tree"])
    assert [*mutations.ENGINE_INPUT_DIRS, "scripts/build"] == listing[-4:]


# ----- scratch paths short enough for a provider's socket


def test_a_scratch_root_that_leaves_room_for_codexs_socket_is_used_as_it_is(tmp_path) -> None:
    root = Path("/tmp/tt")
    assert parallel.short_scratch(root, tmp_path) == (root, None)
    longest_home = f"{'x' * parallel.SCRATCH_ROOT_MAX}/a1001/0f3c/h"
    socket = f"{longest_home}/.longhouse/managed-local/codex-bridge/896ee7fe-04a9-4e26-b166-abd5fdeb2f7a.sock"
    assert len(socket) == parallel.SOCKET_PATH_MAX


def test_the_shard_and_pass_directories_are_as_long_as_the_budget_assumes(tmp_path) -> None:
    runner = parallel.Runner(tmp_path / "out", tmp_path, lambda _m: None)
    assert len(f"/{runner._token}{1:03d}/0f3c/h") == parallel.HOME_TAIL


def test_a_long_scratch_root_gets_a_short_link_named_by_the_root(tmp_path) -> None:
    """CI, 2026-11-13: scratch under /home/runner/_work/tt made every Codex Helm cell fail before it was ready."""

    short = Path("/tmp") / f"q{os.getpid() % 10}"  # stands in for /tmp: the test's own directory is too long to be one
    short.mkdir(exist_ok=True)
    try:
        root = Path("/home/runner/_work/tt")
        assert len(str(root)) > parallel.SCRATCH_ROOT_MAX
        real = tmp_path / "a-scratch-root-that-is-far-too-long-for-a-socket"
        given, link = parallel.short_scratch(real, short)
        assert link == given and given.is_symlink() and given.resolve() == real.resolve() and real.is_dir()
        assert len(str(given)) <= parallel.SCRATCH_ROOT_MAX
        again, _ = parallel.short_scratch(real, short)  # a link a killed run left is replaced
        other, _ = parallel.short_scratch(tmp_path / "another-root-that-is-also-far-too-long", short)
        assert again == given and other != given
        with pytest.raises(SystemExit, match="no scratch path short enough"):
            parallel.short_scratch(real, tmp_path)
    finally:
        shutil.rmtree(short, ignore_errors=True)


def test_a_run_with_a_long_scratch_root_gives_shards_the_link_and_records_and_removes_by_the_root(monkeypatch, tmp_path) -> None:
    link = tmp_path / "ln"
    made: list[Path] = []

    def fake_short(root: Path) -> tuple[Path, Path]:
        link.symlink_to(root, target_is_directory=True)
        made.append(root)
        return link, link

    bad = ("fail", "stream_delivered", "0 of 8 markers")
    plan_names = {m.name: m for m in mutations.MUTATIONS}
    fake = FakeRunner({"long_reply:claude/console": [bad, bad]}, link)
    seen_base: list[Path] = []
    monkeypatch.setattr(parallel, "Runner", lambda _out, base, _log: seen_base.append(base) or fake)
    monkeypatch.setattr(
        parallel,
        "verdict_of",
        lambda unit, row: (
            tuple(row["verdict"])
            if row.get("verdict")
            else (("fail", plan_names[unit.mutation].stages[0], "x") if unit.mutation else ("pass", None, ""))
        ),
    )
    root = tmp_path / "scratch"
    root.mkdir()
    monkeypatch.setattr(parallel, "SCRATCH_ROOT", root)
    monkeypatch.setattr(parallel, "memory_limit", lambda: None)
    monkeypatch.setattr(parallel, "short_scratch", fake_short)
    engine = tmp_path / "longhouse-engine"
    engine.write_text("")
    out = tmp_path / "out"
    code = parallel.main(["--engine", str(engine), "--longhouse-cli", str(engine), "--out-dir", str(out), "--jobs", "4", "--no-mutations"])
    fleet = json.loads((out / "fleet.json").read_text())
    assert code == 1 and seen_base == [link] and made == [root]
    assert not link.is_symlink() and not link.exists()  # the run removed its link
    kept = [Path(k) for k in fleet["kept_scratch"]]
    assert kept and all(k.parent == root and k.is_dir() for k in kept)  # named by the root, and still there
    assert sorted(p.name for p in root.iterdir() if p.name != "server-copies") == sorted(k.name for k in kept)


# ----- the hosts the shards share (HostPool)


class FakeHost:
    """Stands in for ``ScratchRuntimeHost``: ``FakeHost.exit_status`` is what a started host's process reports."""

    started: list[FakeHost] = []
    exit_status: int | None = None
    fail_to_start = False

    def __init__(self, root: Path, extra_environment: dict | None = None, *, device_id: str = "terminal-truth") -> None:
        self.root, self.log_path, self.device_id = root, root / "runtime-host.log", device_id
        self.url, self.process, self.stopped = "", None, False

    def start(self) -> None:
        if FakeHost.fail_to_start:
            raise RuntimeError("scratch Runtime Host did not answer /api/health")
        status = FakeHost.exit_status
        self.url = f"http://127.0.0.1:{len(FakeHost.started) + 1}"
        self.process = SimpleNamespace(poll=lambda: status)
        self.log_path.write_text("up\n")
        FakeHost.started.append(self)

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def fake_hosts(monkeypatch):
    FakeHost.started, FakeHost.exit_status, FakeHost.fail_to_start = [], None, False
    monkeypatch.setattr(parallel, "ScratchRuntimeHost", FakeHost)
    return FakeHost


def test_hosts_start_when_a_shard_needs_one_and_each_carries_so_many_shards_at_a_time(fake_hosts, tmp_path) -> None:
    """Until 2026-11-13 every pass started its own host: 6 to 8 s and 0.9 GiB, about 80 times a run."""

    pool = parallel.HostPool(tmp_path / "hosts", 2, lambda _m: None)
    assert fake_hosts.started == []  # nothing is started for a run that never asks
    a, b = pool.take(), pool.take()
    assert a is b and len(fake_hosts.started) == 1 and a.host.root == tmp_path / "hosts" / "host-0"
    c = pool.take()  # the first host is full
    assert c is not a and [h.name for h in pool.hosts] == ["host-0", "host-1"]
    pool.give_back(a)
    assert pool.take() is a  # room again on the first
    assert pool.take() is c and len(fake_hosts.started) == 2  # the least busy host with room, not a third
    state = pool.stop()
    assert all(h.stopped for h in fake_hosts.started)
    assert [(h["name"], h["shards"], h["exited"], h["error"]) for h in state] == [("host-0", 3, None, ""), ("host-1", 2, None, "")]


def test_a_host_that_exited_or_never_started_is_not_handed_out_again_and_is_reported(fake_hosts, tmp_path) -> None:
    pool = parallel.HostPool(tmp_path / "hosts", 4, lambda _m: None)
    fake_hosts.exit_status = -9
    dead = pool.take()
    pool.give_back(dead)
    fake_hosts.exit_status = None
    alive = pool.take()
    assert alive is not dead and alive.name == "host-1"
    fake_hosts.fail_to_start = True
    pool.per_host = 1
    never = pool.take()  # the shard given it fails with the reason; the run goes on
    assert never.error == "RuntimeError: scratch Runtime Host did not answer /api/health" and never.up.is_set()
    fake_hosts.fail_to_start = False
    assert pool.take().name == "host-3"
    assert [(h["name"], h["exited"], bool(h["error"])) for h in pool.stop()] == [
        ("host-0", -9, False),
        ("host-1", None, False),
        ("host-2", None, True),
        ("host-3", None, False),
    ]


def test_shards_asking_at_once_wait_for_the_one_host_that_is_starting(fake_hosts, tmp_path, monkeypatch) -> None:
    release = parallel.threading.Event()
    slow_start = fake_hosts.start
    monkeypatch.setattr(fake_hosts, "start", lambda self: (release.wait(5), slow_start(self))[1])
    pool = parallel.HostPool(tmp_path / "hosts", 2, lambda _m: None)
    got: list[parallel.PooledHost] = []
    threads = [parallel.threading.Thread(target=lambda: got.append(pool.take())) for _ in range(2)]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    assert got == [] and len(pool.hosts) == 1  # both were given the one host and neither has it before it is up
    release.set()
    for thread in threads:
        thread.join(timeout=5)
    assert len(got) == 2 and got[0] is got[1] and got[0].host.url


def test_a_shard_is_pointed_at_a_shared_host_unless_it_needs_a_host_of_its_own(fake_hosts, monkeypatch, tmp_path) -> None:
    seen: list[dict[str, str]] = []

    class Child:
        def __init__(self, argv, *, env, **_kwargs) -> None:
            seen.append(env)

        def wait(self, timeout=None) -> int:
            return 0

    monkeypatch.setattr(parallel.subprocess, "Popen", Child)
    monkeypatch.setattr(parallel, "leaked_processes", lambda _root: [])
    monkeypatch.delenv("TT_RUNTIME_HOST", raising=False)
    runner = parallel.Runner(tmp_path / "out", tmp_path / "scratch", lambda _m: None)
    runner.hosts = parallel.HostPool(tmp_path / "scratch" / "hosts", 2, lambda _m: None)
    unit = parallel.Unit("long_reply:claude/console", "claude/console", "long_reply")
    shared = runner.run(parallel.Shard("a", "long_reply", [unit], ENGINE, CLI))
    own = runner.run(parallel.Shard("b", "long_reply", [unit], ENGINE, CLI, ["--host-env", "PYTHONPATH=/a/patched/server"]))
    host = fake_hosts.started[0]
    assert (seen[0]["TT_RUNTIME_HOST"], seen[0]["TT_RUNTIME_HOST_LOG"]) == (host.url, str(host.log_path))
    assert "TT_RUNTIME_HOST" not in seen[1] and (shared.host, own.host) == ("host-0", None)
    assert [h.shards for h in runner.hosts.hosts] == [0]  # given back when the shard ended


def test_a_run_shares_hosts_two_shards_to_one_and_that_is_the_gate(monkeypatch, tmp_path) -> None:
    code, fleet, fake = run_with(monkeypatch, tmp_path, {})
    assert code == 0 and fleet["narrowed"] is False and fleet["shards_per_host"] == parallel.SHARDS_PER_HOST == 2
    assert isinstance(fake.hosts, parallel.HostPool) and fake.hosts.per_host == 2 and fleet["hosts"] == []
    code, fleet, fake = run_with(monkeypatch, tmp_path / "own", {}, "--shards-per-host", "0")
    assert code == 0 and fake.hosts is None and fleet["shards_per_host"] == 0


def run_on_hosts(monkeypatch, tmp_path, script) -> tuple[int, dict]:
    """A run whose every shard takes a shared host, as the real runner's shards do."""

    plain = FakeRunner.run

    def run(self, shard, attempt=""):
        pooled = self.hosts.take()
        try:
            result = plain(self, shard, attempt)
            result.host = pooled.name
            return result
        finally:
            self.hosts.give_back(pooled)

    monkeypatch.setattr(FakeRunner, "run", run)
    code, fleet, _fake = run_with(monkeypatch, tmp_path, script)
    return code, fleet


def test_a_shared_host_that_exited_fails_the_run_and_its_log_is_kept(fake_hosts, monkeypatch, tmp_path) -> None:
    fake_hosts.exit_status = -9
    code, fleet = run_on_hosts(monkeypatch, tmp_path, {})
    assert code == 1 and fleet["state"] == "failure" and fleet["hosts"][0]["exited"] == -9
    assert fleet["hosts"][0]["root"] in fleet["kept_scratch"] and fake_hosts.started[0].log_path.is_file()


def test_a_wrong_row_keeps_the_log_of_the_host_it_ran_on_and_a_clean_run_keeps_none(fake_hosts, monkeypatch, tmp_path) -> None:
    bad = ("fail", "state_shown", "2.6 s behind")
    code, fleet = run_on_hosts(monkeypatch, tmp_path, {"thinking:claude/console": [bad, bad]})
    used = {shard["host"] for shard in fleet["shards"] if "thinking" in shard["name"] and "claude/console" in " ".join(shard["cells"])}
    roots = {h["name"]: h["root"] for h in fleet["hosts"]}
    assert code == 1 and used and all(roots[name] in fleet["kept_scratch"] and Path(roots[name]).is_dir() for name in used)

    fake_hosts.started.clear()
    code, fleet = run_on_hosts(monkeypatch, tmp_path / "clean", {})
    assert code == 0 and fleet["kept_scratch"] == [] and fleet["hosts"] and all(h.stopped for h in fake_hosts.started)
    assert not (tmp_path / "clean" / "scratch" / "hosts").exists()


def test_claude_is_onboarded_once_a_run_and_later_passes_copy_the_profile_under_their_own_paths(monkeypatch, tmp_path) -> None:
    """A run onboarded Claude in every pass with a Claude cell: 13 s of first-run screens, about twenty times (2026-11-13)."""
    from zerg.qa import live_session_toolkit as toolkit
    from zerg.qa import terminal_truth_providers as providers

    onboarded: list[Path] = []

    def prepare(*, binary, home, workspace, environment, recording) -> None:
        config = home / ".claude"
        (config / "projects").mkdir(parents=True)
        environment["CLAUDE_CONFIG_DIR"] = str(config)
        trusted = {str(workspace.resolve()): {"hasTrustDialogAccepted": True}}
        (config / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True, "projects": trusted, "given": str(workspace)}))
        (config / "blob.bin").write_bytes(b"\xff\xfe\x00")
        onboarded.append(home)

    def a_pass(name: str) -> tuple[Path, dict[str, str], str]:
        real = tmp_path / "real" / name
        (real / "h").mkdir(parents=True)
        (real / "w" / "claude").mkdir(parents=True)
        (tmp_path / "links").mkdir(exist_ok=True)
        root = tmp_path / "links" / name  # the scratch root is a short link on a runner
        root.symlink_to(real, target_is_directory=True)
        penv: dict[str, str] = {}
        how = providers.onboard_claude(Path("claude"), home=root / "h", root=root, penv=penv, workspace_root=root / "w" / "claude")
        return root, penv, how

    monkeypatch.setattr(toolkit, "prepare_claude_profile", prepare)
    monkeypatch.delenv(providers.PROFILE_CACHE_ENV, raising=False)
    assert [a_pass("solo-1")[2], a_pass("solo-2")[2]] == ["onboarded", "onboarded"] and len(onboarded) == 2  # no cache: as before

    monkeypatch.setenv(providers.PROFILE_CACHE_ENV, str(tmp_path / "profiles"))
    first, _penv, how = a_pass("a1001")
    assert how == "onboarded" and len(onboarded) == 3
    second, penv, how = a_pass("b2002")
    assert how == "copied" and len(onboarded) == 3
    config = second / "h" / ".claude"
    profile = json.loads((config / ".claude.json").read_text())
    assert penv["CLAUDE_CONFIG_DIR"] == str(config) and (config / "blob.bin").read_bytes() == b"\xff\xfe\x00"
    # The folder Claude trusts is this pass's, in both spellings, and nothing names the first pass any more.
    assert profile["projects"] == {str((second / "w" / "claude").resolve()): {"hasTrustDialogAccepted": True}}
    assert profile["given"] == str(second / "w" / "claude") and "a1001" not in (config / ".claude.json").read_text()

    # A copy that was cut short (no made.json) is not used: the next pass onboards again.
    (tmp_path / "profiles" / "claude" / "made.json").unlink()
    assert a_pass("c3003")[2] == "onboarded" and len(onboarded) == 4


def test_a_pass_attaches_to_the_runs_host_unless_it_has_a_host_environment_of_its_own(monkeypatch, tmp_path) -> None:
    from zerg.qa import terminal_truth_providers as providers
    from zerg.qa import terminal_truth_scenarios as scenarios

    root = tmp_path / "a1001" / "0f3c"
    monkeypatch.delenv("TT_RUNTIME_HOST", raising=False)
    assert isinstance(scenarios.pass_host(root, None), providers.ScratchRuntimeHost)
    monkeypatch.setenv("TT_RUNTIME_HOST", "http://127.0.0.1:1/")
    monkeypatch.setenv("TT_RUNTIME_HOST_LOG", str(tmp_path / "shared-host" / "runtime-host.log"))
    attached = scenarios.pass_host(root, None)
    assert isinstance(attached, providers.AttachedRuntimeHost)
    assert (attached.url, attached.log_path, attached.device_id) == (
        "http://127.0.0.1:1",
        tmp_path / "shared-host" / "runtime-host.log",
        "tt-a1001-0f3c",
    )
    patched = scenarios.pass_host(root, {"PYTHONPATH": "/a/patched/server"})  # a mutation test's server: its own host
    assert isinstance(patched, providers.ScratchRuntimeHost) and patched.device_id == "terminal-truth"


def test_an_attached_host_takes_a_token_for_its_own_device_and_is_not_stopped_by_the_pass(monkeypatch, tmp_path) -> None:
    from zerg.qa import terminal_truth_providers as providers

    minted: list[tuple[str, dict]] = []
    monkeypatch.setattr(providers.urllib.request, "urlopen", lambda url, timeout=0: contextlib.nullcontext())
    monkeypatch.setattr(providers, "_post_json", lambda url, payload: minted.append((url, payload)) or {"token": "t-1"})
    host = providers.AttachedRuntimeHost("http://127.0.0.1:1", tmp_path / "runtime-host.log", "tt-a1001-0f3c")
    host.start()
    host.stop()
    assert host.token == "t-1" and minted == [
        ("http://127.0.0.1:1/api/devices/tokens", {"device_id": "tt-a1001-0f3c", "name": "tt-a1001-0f3c"})
    ]


def test_a_pass_keeps_asking_a_busy_shared_host_and_gives_up_on_any_other_refusal(monkeypatch, tmp_path) -> None:
    """Bench, 2026-11-13, twelve shards on six hosts: a host answered one pass's first request with 503 and the pass failed its setup."""
    import urllib.error

    from zerg.qa import terminal_truth_providers as providers

    answers = [503, 503, 200]

    def post(url: str, payload: dict) -> dict:
        status = answers.pop(0)
        if status != 200:
            raise urllib.error.HTTPError(url, status, "refused", None, None)  # type: ignore[arg-type]
        return {"token": "t-2"}

    monkeypatch.setattr(providers.urllib.request, "urlopen", lambda url, timeout=0: contextlib.nullcontext())
    monkeypatch.setattr(providers, "_post_json", post)
    monkeypatch.setattr(providers.time, "sleep", lambda _s: None)
    host = providers.AttachedRuntimeHost("http://127.0.0.1:1", tmp_path / "runtime-host.log", "tt-a1001-0f3c")
    host.start()
    assert host.token == "t-2" and answers == []

    answers[:] = [500]
    with pytest.raises(urllib.error.HTTPError):
        host.start()
    monkeypatch.setattr(providers, "ATTACH_WAIT_S", 0.0)  # a host that stays busy is a failure, not a hang
    answers[:] = [503, 200]
    with pytest.raises(urllib.error.HTTPError):
        host.start()


def test_the_memory_gate_records_the_highest_use_it_saw_and_nothing_where_it_cannot_read(monkeypatch) -> None:
    readings = iter([1 * 2**30, 3 * 2**30, 2 * 2**30])
    monkeypatch.setattr(parallel, "_memory_in_use", lambda: next(readings, 2 * 2**30))
    watch = parallel.MemoryGate(None)
    watch.read()
    watch.read()
    assert watch.finish() == {"before_gib": 1.0, "peak_gib": 3.0}
    monkeypatch.setattr(parallel, "_memory_in_use", lambda: None)
    assert parallel.MemoryGate(None).finish() is None
