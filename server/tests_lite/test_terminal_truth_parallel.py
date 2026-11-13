"""The all-checks run: what it plans, how it cuts shards, how it judges rows and which rows it runs again. No provider runs."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from zerg.qa import terminal_truth_mutations as mutations
from zerg.qa import terminal_truth_parallel as parallel
from zerg.qa.terminal_truth_kinds import KIND_EXPECTATIONS
from zerg.qa.terminal_truth_kinds import KNOWN_LATE_STATE
from zerg.qa.terminal_truth_kinds import kinds_verdict
from zerg.qa.terminal_truth_verdicts import declared_cells
from zerg.qa.terminal_truth_verdicts import load_expectations

ENGINE, CLI = Path("/e/longhouse-engine"), Path("/e/longhouse")


def full_plan(**overrides) -> parallel.Plan:
    options = {"scenarios": parallel.ALL_SCENARIOS, "providers": None, "modes": None, "with_mutations": True, "only": None}
    return parallel.make_plan(**{**options, **overrides})


def shards_of(plan: parallel.Plan, providers_per_shard: int = 1) -> list[parallel.Shard]:
    engines = {m.name: (Path(f"/m/{m.name}/longhouse-engine"), Path(f"/m/{m.name}/longhouse")) for m in plan.selected if m.side == "engine"}
    copies = {m.name: Path(f"/c/{m.name}/server") for m in plan.selected if m.side == "server"}
    return parallel.make_shards(
        plan, engine=ENGINE, cli=CLI, providers_per_shard=providers_per_shard, tokens=100, rate=20.0, mutated_engines=engines, server_copies=copies
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


def test_a_mutated_run_gets_its_own_shard_with_the_mutated_engine_or_the_patched_server() -> None:
    plan = full_plan()
    by_unit = {s.units[0].id: s for s in shards_of(plan) if len(s.units) == 1}
    for m in mutations.MUTATIONS:
        shard = by_unit[f"mutated:{m.name}"]
        if m.side == "server":
            assert (shard.engine, shard.cli) == (ENGINE, CLI)
            assert shard.flags[-2:] == ["--host-env", f"PYTHONPATH=/c/{m.name}/server"]
        else:
            assert shard.engine == Path(f"/m/{m.name}/longhouse-engine") and shard.cli == Path(f"/m/{m.name}/longhouse")
            assert "--host-env" not in shard.flags
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
        self.stopped = parallel.threading.Event()

    def run(self, shard: parallel.Shard, attempt: str = "") -> parallel.ShardRun:
        self.calls.append((shard.name, attempt, [u.id for u in shard.units]))
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
    monkeypatch.setattr(parallel, "memory_room_gib", lambda: None)
    built = {"hits": 0, "built": 0, "seconds": 0.0, "inputs_digest": "x"}
    engines = {m.name: (ENGINE, CLI) for m in mutations.MUTATIONS if m.side == "engine"}
    monkeypatch.setattr(
        mutations, "build_mutated_engines", lambda selected, **k: ({m.name: engines[m.name] for m in selected if m.side == "engine"}, built)
    )
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
    engines = {m.name: (ENGINE, CLI) for m in plan.selected if m.side == "engine"}
    copies = {m.name: tmp_path / m.name for m in plan.selected if m.side == "server"}
    cached = parallel.make_shards(
        plan, engine=ENGINE, cli=CLI, providers_per_shard=2, tokens=100, rate=20.0, mutated_engines=engines, server_copies=copies,
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


def test_fewer_shards_run_at_once_when_the_memory_limit_holds_fewer_and_never_none() -> None:
    assert parallel.fit_jobs(8, None) == (8, "")
    assert parallel.fit_jobs(8, (64.0, "a limit of 64.0 GiB")) == (8, "")
    jobs, why = parallel.fit_jobs(8, (7.6, "a limit of 8.0 GiB with 0.4 GiB in use"))
    assert jobs == int((7.6 - parallel.RESERVED_MEMORY_GIB) / parallel.SHARD_MEMORY_GIB) == 4
    assert "4 at a time, not 8" in why and "a limit of 8.0 GiB with 0.4 GiB in use" in why
    assert parallel.fit_jobs(8, (0.5, "0.5 GiB available on the machine"))[0] == 1
    assert parallel.fit_jobs(2, (7.6, "x")) == (2, "")


def test_a_run_that_was_given_less_memory_says_so_and_records_both_numbers(monkeypatch, tmp_path, capsys) -> None:
    code, fleet, _fake = run_with(monkeypatch, tmp_path, {})
    monkeypatch.setattr(parallel, "memory_room_gib", lambda: (5.0, "a limit of 5.0 GiB with 0.0 GiB in use"))
    out = tmp_path / "second"
    engine = tmp_path / "longhouse-engine"
    code = parallel.main(["--engine", str(engine), "--longhouse-cli", str(engine), "--out-dir", str(out), "--jobs", "8", "--no-mutations"])
    fleet = json.loads((out / "fleet.json").read_text())
    assert code == 0 and (fleet["jobs"], fleet["jobs_asked"]) == (2, 8)
    assert "2 at a time, not 8: a limit of 5.0 GiB" in capsys.readouterr().err


# ----- the mutated engine cache


def test_the_cache_key_changes_with_the_engine_inputs_and_with_the_edit_and_not_with_the_name() -> None:
    m = mutations.MUTATIONS[0]
    key = mutations.mutated_engine_key(m, "a" * 64)
    assert re.fullmatch(r"[0-9a-f]{24}", key)
    assert mutations.mutated_engine_key(m, "b" * 64) != key
    assert mutations.mutated_engine_key(mutations.dataclasses.replace(m, new=m.new + " "), "a" * 64) != key
    assert mutations.mutated_engine_key(mutations.dataclasses.replace(m, name="renamed"), "a" * 64) == key


def test_cached_engines_are_not_rebuilt_and_a_full_run_drops_entries_of_another_engine_version(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setattr(mutations, "engine_inputs_digest", lambda: "d" * 64)
    monkeypatch.setattr(mutations, "SCRATCH", tmp_path / "tt-mut")
    monkeypatch.setattr(mutations, "_run", lambda *a, **k: pytest.fail("a cached engine was rebuilt"))
    engine_side = [m for m in mutations.MUTATIONS if m.side == "engine"]
    for m in engine_side:
        entry = cache / mutations.mutated_engine_key(m, "d" * 64)
        entry.mkdir(parents=True, exist_ok=True)  # two mutations that are one edit on two cells share one binary
        for binary in mutations.BINARIES:
            (entry / binary).write_text("")
    stale = cache / ("0" * 24)
    stale.mkdir()
    unrelated = cache / "README"
    unrelated.write_text("not ours")

    narrowed, stats = mutations.build_mutated_engines(engine_side[:1], cache=cache, cargo_target=tmp_path / "t")
    assert stats["hits"] == 1 and stats["built"] == 0 and stale.exists()  # a narrowed run empties nothing

    engines, stats = mutations.build_mutated_engines(list(mutations.MUTATIONS), cache=cache, cargo_target=tmp_path / "t")
    assert stats["hits"] == len({mutations.mutated_engine_key(m, "d" * 64) for m in engine_side}) and stats["built"] == 0
    assert set(engines) == {m.name for m in engine_side} and all(e.is_file() and c.is_file() for e, c in engines.values())
    assert not stale.exists() and unrelated.exists()


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


def test_the_engine_is_built_once_per_set_of_inputs_and_copied_from_the_cache_after(monkeypatch, tmp_path) -> None:
    from zerg.qa import terminal_truth_engine as engine

    builds: list[int] = []

    def fake_build() -> dict[str, Path]:
        builds.append(1)
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

    assert engine.provide(tmp_path / "one", cache) == "miss"
    assert engine.provide(tmp_path / "two", cache) == "hit" and len(builds) == 1
    assert (tmp_path / "two" / "longhouse-engine").read_text() == "longhouse-engine build 1"
    assert engine.provide(tmp_path / "three", None) == "off" and len(builds) == 2

    digest[0] = "b" * 64  # the engine sources changed
    assert engine.provide(tmp_path / "four", cache) == "miss" and len(builds) == 3
    assert [entry.name for entry in cache.iterdir()] == ["b" * 24]  # one build is kept


def test_the_engine_and_the_mutated_engines_are_keyed_by_different_flags_over_the_same_inputs(monkeypatch) -> None:
    from zerg.qa import terminal_truth_engine as engine

    seen: list[list[str]] = []

    def fake_run(command, **_kwargs):
        seen.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="tree-or-rustc\n", stderr="")

    monkeypatch.setattr(mutations.subprocess, "run", fake_run)
    mutated, measured = mutations.engine_inputs_digest(), mutations.engine_inputs_digest(flags=engine.FLAGS)
    assert mutated != measured
    listing = next(command for command in seen if command[:2] == ["git", "ls-tree"])
    assert [*mutations.ENGINE_INPUT_DIRS, "scripts/build"] == listing[-4:]
