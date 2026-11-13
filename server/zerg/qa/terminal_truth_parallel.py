"""Terminal truth, every check in one run: all cells of all five scenarios side by side, then the mutation tests.

A cell is 20 to 40 seconds of wall time and almost all of it is waiting: for a scripted stream paced in real time, for a
provider to start, for a host to boot. Run one after another, the 58 unmutated cell runs and the 43 mutation checks are an
hour and a half (2026-11-13: 13 minutes for the reply cells of a push, 75 for the weekly mutation run). Nothing in a cell
needs another cell to have finished, so this command runs them at the same time:

- A **shard** is one ordinary ``terminal_truth run`` process on a few cells: its own Runtime Host, Machine Agent, scripted
  model and HOME under its own scratch root. The cell code and its verdict are the ones the serial command runs.
- Every unmutated cell runs once. A mutation test reads its unmutated half from that run (same cell, scenario and
  options) and runs only its mutated half, with a mutated engine from the build cache
  (``terminal_truth_mutations.build_mutated_engines``) or a patched copy of the server package.
- Anything that did not come out as wanted is then run once more **alone**, with nothing else running. A defect fails
  both times; a cell that was slow only because eleven others were starting passes the second, and the row says so
  (``reruns`` in fleet.json, a warning in CI). More than ``MAX_RERUNS`` such rows is not noise and nothing is rerun.

Every shard's Runtime Host runs without the embedding lane (recall is not what a cell measures; the lane is 0.9 GiB and
four inference threads per host) except the one shard named by ``EMBEDDING_SHARD``, so that a change which lets that
lane hold up live delivery still fails a cell. ``--jobs`` is lowered to what the memory limit holds (``fit_jobs``).

Each shard has its own host because one host does not carry many cells. ``--shared-host`` starts one Runtime Host for
the run and every shard's Machine Agent connects to it under its own device id, as machines do in production. Measured
2026-11-13 on a Linux bench with the Codex and Claude cells (a cell asks its host about 20 reads a second): one shard at
a time, every cell passed and a session read took 51 ms at the median and 117 ms at the 90th percentile; two at a
time, every cell passed, 289 ms at the 90th and 15 requests refused; three at a time, 4 of 20 cells failed (state shown
1 to 2.6 s late), 612 ms at the 90th and 22 refused; six at a time, 4 of 20 failed and 4 in 10 reads took over half a
second. The refusals are the catalog's own limit of two reads at once ("catalog read lane is full"). The option stays as
the way to measure what one host carries.

Scratch paths must be short: Codex binds ``<home>/.longhouse/managed-local/codex-bridge/<uuid>.sock`` and a socket path
holds 107 bytes on Linux, which leaves the home 27 (``short_scratch``; a CI run on 2026-11-13 failed every Codex Helm
cell with a home of 34). Scratch must also be on a filesystem where ``fsync`` is cheap (the CI job points TT_SCRATCH_ROOT at its RAM-backed work
volume). Measured 2026-11-13 on a Linux bench, six shards at once, twelve reply cells: on a busy NVMe three Console cells
failed with the Machine Agent holding live text for 3 to 10 seconds; with scratch in memory all twelve passed with live
medians of 0.02 to 0.21 s.

    make terminal-truth-all ARGS="--engine <engine> --longhouse-cli <longhouse> --out-dir DIR"

Writes, in --out-dir: ``pass-<i>-<scenario>.json`` (reply cells, the serial command's rows), ``kinds.json`` (one verdict
row per thinking, tool and subagent cell) with the full results in ``kinds-<scenario>.json``, ``mutations.json`` (the
rows of the mutation table), ``fleet.json`` (the plan, every shard's time and exit, the reruns, kept scratch) and
``shards/`` (each shard's log and result). Exits 1 unless every planned item has a good verdict.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from zerg.qa import terminal_truth_mutations as mutations
from zerg.qa.terminal_truth_kinds import KIND_EXPECTATIONS
from zerg.qa.terminal_truth_kinds import KNOWN_LATE_STATE
from zerg.qa.terminal_truth_kinds import SCENARIOS as KIND_SCENARIOS
from zerg.qa.terminal_truth_kinds import kinds_verdict
from zerg.qa.terminal_truth_providers import ScratchRuntimeHost
from zerg.qa.terminal_truth_pty import REPO_ROOT
from zerg.qa.terminal_truth_pty import SCRATCH_ROOT
from zerg.qa.terminal_truth_scenarios import leaked_processes
from zerg.qa.terminal_truth_verdicts import declared_cells
from zerg.qa.terminal_truth_verdicts import load_expectations

SERVER_ROOT = REPO_ROOT / "server"
REPLY_SCENARIOS = ("long_reply", "two_message")
ALL_SCENARIOS = REPLY_SCENARIOS + tuple(KIND_SCENARIOS)
GOOD = {"pass", "known_bad", "not_measurable"}
# Shards are cut along this order. OMP and Pi are far apart: in one HOME each breaks the other's launch, so a shard that
# holds both runs them as two passes one after the other (run_suite, run_kinds_suite) and takes twice as long.
PROVIDER_ORDER = ("claude", "omp", "codex", "antigravity", "opencode", "pi", "cursor")
#: More rows than this to run again is a broken product or a sick runner, not one slow cell.
MAX_RERUNS = 8
# The unmutated shard whose host runs the embedding lane: the reply cells of the provider used most.
EMBEDDING_SHARD = ("long_reply", "claude")
EMBEDDING_ON = "LONGHOUSE_EMBEDDING_PROJECTOR_DISABLED=0"
EMBEDDING_LOADED = "Local embedder loaded"  # the host's log line once the model is in memory
# Memory, measured 2026-11-13 on a Linux bench (sum of resident memory of everything a one-cell shard starts): 1.36 GiB,
# and 2.16 GiB with the embedding lane. ``fit_jobs`` budgets a little more per shard and sets aside the lane's extra.
SHARD_MEMORY_GIB = 1.5
RESERVED_MEMORY_GIB = 1.5
# With --shared-host a shard has no host of its own (0.9 GiB less) and the one host has the embedding model.
SHARED_HOST_SHARD_MEMORY_GIB = 0.6
SHARED_HOST_RESERVED_MEMORY_GIB = 2.5
# The longest socket path a provider binds under its home is Codex's bridge: this much after the home directory.
SOCKET_PATH_MAX = 103 if sys.platform == "darwin" else 107
CODEX_SOCKET_TAIL = len("/.longhouse/managed-local/codex-bridge/") + 36 + len(".sock")
HOME_TAIL = len("/00000/0000/h")  # under the scratch root: the shard's directory, the pass's, the home
SCRATCH_ROOT_MAX = SOCKET_PATH_MAX - CODEX_SOCKET_TAIL - HOME_TAIL
SHARD_TIMEOUT_S = 900.0
EXTRA_CHECKS = {
    # name -> (what the table calls it, cell, flags, the verdict it must have)
    "reworded-ready-string": ("reworded ready string", "codex/helm", ("--ready-text", "codex=Ask Codex to do anything, v2", "--ready-timeout", "12"), "preflight"),
    "half-speed": ("half-speed fake model", "codex/helm", (), "(pass)"),
}  # fmt: skip


@dataclass(frozen=True)
class Options:
    """What a live-kinds cell is run with. Two runs of one cell are the same check only when these are equal."""

    warm: bool = False
    text_before_tool: bool = False
    engine_delay_ms: int = 0

    def flags(self) -> list[str]:
        out = ["--warm-session"] if self.warm else []
        out += ["--text-before-tool"] if self.text_before_tool else []
        out += ["--engine-delay-ms", str(self.engine_delay_ms)] if self.engine_delay_ms else []
        return out

    def suffix(self) -> str:
        parts = (["cold"] if not self.warm else []) + (["text-before-tool"] if self.text_before_tool else [])
        parts += [f"delay{self.engine_delay_ms}"] if self.engine_delay_ms else []
        return "+".join(parts)


def standard_options(cell: str, scenario: str) -> Options:
    """How the gate runs a cell: a Helm live-kinds cell with its session open first (KIND_EXPECTATIONS says why)."""

    return Options(warm=scenario in KIND_SCENARIOS and cell.endswith("/helm"))


def unit_id(scenario: str, cell: str, options: Options) -> str:
    extra = "" if options == standard_options(cell, scenario) else f"+{options.suffix()}"
    return f"{scenario}:{cell}{extra}"


@dataclass
class Unit:
    """One cell run: an unmutated cell, the mutated half of a mutation test, or one of the two checks with no edit."""

    id: str
    cell: str
    scenario: str
    options: Options = Options()
    mutation: str | None = None  # the mutation whose defect this run carries
    extra: str | None = None  # a key of EXTRA_CHECKS

    @property
    def kinds(self) -> bool:
        return self.scenario in KIND_SCENARIOS


@dataclass
class Shard:
    name: str
    scenario: str
    units: list[Unit]
    engine: Path
    cli: Path
    flags: list[str] = field(default_factory=list)
    embedding: bool = False  # its host runs the embedding lane

    @property
    def weight(self) -> float:
        """A rough length, for starting the long ones first (the shortest makespan with a fixed number of workers)."""

        per_cell = {"long_reply": 22, "two_message": 30, "thinking": 30, "tool": 30, "subagent": 45}[self.scenario]
        return 20 + per_cell * len(self.units) + (15 if any(u.cell.startswith("claude/") for u in self.units) else 0)


@dataclass
class Plan:
    units: dict[str, Unit]  # every unmutated cell the run judges, by id
    baseline_of: dict[str, str]  # mutation name -> id of its unmutated unit
    mutated: dict[str, Unit]  # mutation name -> its mutated unit
    extras: dict[str, Unit]
    selected: list[mutations.Mutation]


def make_plan(
    *,
    scenarios: Sequence[str],
    providers: Sequence[str] | None,
    modes: Sequence[str] | None,
    with_mutations: bool,
    only: Sequence[str] | None,
    expectations: dict[tuple[str, str], Any] | None = None,
) -> Plan:
    expectations = expectations if expectations is not None else load_expectations()

    def wanted(cell: str) -> bool:
        provider, mode = cell.split("/")
        return (not providers or provider in providers) and (not modes or mode in modes)

    units: dict[str, Unit] = {}
    for scenario in scenarios:
        if scenario in REPLY_SCENARIOS:
            cells = [spec.key for spec in declared_cells(expectations)]
        else:
            # Only cells with a published-kind expectation: the others are measurements with nothing to pass or fail.
            cells = sorted(cell for (cell, sc) in KIND_EXPECTATIONS if sc == scenario)
        for cell in filter(wanted, cells):
            options = standard_options(cell, scenario)
            units[unit_id(scenario, cell, options)] = Unit(unit_id(scenario, cell, options), cell, scenario, options)
    selected: list[mutations.Mutation] = []
    baseline_of: dict[str, str] = {}
    mutated: dict[str, Unit] = {}
    extras: dict[str, Unit] = {}
    if with_mutations:
        selected = [m for m in mutations.MUTATIONS if not only or m.name in only]
        for m in selected:
            scenario = m.scenario or "long_reply"
            options = Options(m.warm, m.text_before_tool, m.engine_delay_ms) if m.scenario else Options()
            if m.cell.endswith("/console"):
                options = dataclasses.replace(options, warm=False)  # a Console cell has no terminal to warm
            base = unit_id(scenario, m.cell, options)
            # The unmutated half: the gate's own run of that cell, or one more run when the mutation needs other options
            # (or the cell was narrowed out of this run).
            units.setdefault(base, Unit(base, m.cell, scenario, options))
            baseline_of[m.name] = base
            mutated[m.name] = Unit(f"mutated:{m.name}", m.cell, scenario, options, mutation=m.name)
        for name, (_label, cell, _flags, _want) in EXTRA_CHECKS.items():
            if not only or name in only:
                extras[name] = Unit(f"extra:{name}", cell, "long_reply", extra=name)
    return Plan(units, baseline_of, mutated, extras, selected)


def make_shards(
    plan: Plan,
    *,
    engine: Path,
    cli: Path,
    providers_per_shard: int,
    tokens: int,
    rate: float,
    mutated_engines: dict[str, tuple[Path, Path]],
    server_copies: dict[str, Path],
    embed_model_dir: Path | None = None,
) -> list[Shard]:
    """Unmutated cells in shards of a few providers per scenario; every other run in a shard of its own."""

    reply_flags = ["--tokens", str(tokens), "--rate", str(rate)]
    shards: list[Shard] = []
    grouped: dict[tuple[str, Options | None, int], list[Unit]] = {}
    rank = {p: i for i, p in enumerate(PROVIDER_ORDER)}
    for unit in plan.units.values():
        provider = unit.cell.split("/")[0]
        if unit.options != standard_options(unit.cell, unit.scenario):
            shards.append(Shard(_safe(unit.id), unit.scenario, [unit], engine, cli, unit.options.flags()))
            continue
        slot = rank.get(provider, len(rank)) // max(1, providers_per_shard)
        grouped.setdefault((unit.scenario, None, slot), []).append(unit)
    for (scenario, _options, slot), members in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][2])):
        members.sort(key=lambda u: (rank.get(u.cell.split("/")[0], 99), u.cell))
        flags = reply_flags if scenario in REPLY_SCENARIOS else (["--warm-session"] if any(u.options.warm for u in members) else [])
        shard = Shard(f"{scenario}-{slot}", scenario, members, engine, cli, list(flags))
        if scenario == EMBEDDING_SHARD[0] and any(u.cell.startswith(EMBEDDING_SHARD[1] + "/") for u in members):
            shard.embedding = True
            shard.flags += ["--host-env", EMBEDDING_ON]
            if embed_model_dir is not None:  # a checksum-verified copy that outlives the checkout
                shard.flags += ["--host-env", f"LONGHOUSE_EMBED_MODEL_DIR={embed_model_dir}"]
        shards.append(shard)
    for name, unit in plan.mutated.items():
        mutation = next(m for m in plan.selected if m.name == name)
        flags = reply_flags if not unit.kinds else unit.options.flags()
        if mutation.side == "server":
            flags = [*flags, "--host-env", f"PYTHONPATH={server_copies[name]}"]
            shards.append(Shard(_safe(unit.id), unit.scenario, [unit], engine, cli, flags))
        else:
            mutated_engine, mutated_cli = mutated_engines[name]
            shards.append(Shard(_safe(unit.id), unit.scenario, [unit], mutated_engine, mutated_cli, list(flags)))
    for name, unit in plan.extras.items():
        _label, _cell, flags, _want = EXTRA_CHECKS[name]
        pace = ["--tokens", str(tokens), "--rate", str(rate / 2 if name == "half-speed" else rate)]
        shards.append(Shard(_safe(unit.id), unit.scenario, [unit], engine, cli, [*pace, *flags]))
    return shards


def _safe(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in text)


@dataclass
class ShardRun:
    shard: Shard
    rows: dict[str, dict[str, Any]]  # unit id -> the cell's result row
    exit: int | None
    seconds: float
    started_s: float
    scratch: Path
    log: Path
    note: str = ""
    keep: bool = True  # its scratch (recordings, host and engine logs) stays for the receipt
    embedding_loaded: bool | None = None  # for the shard that runs the embedding lane: did the model load


class Runner:
    """Runs shards as child processes, each in its own session and scratch root, and reads back one row per unit."""

    def __init__(self, out_dir: Path, scratch_base: Path, log: Callable[[str], None]) -> None:
        self.out_dir, self.scratch_base, self.log = out_dir, scratch_base, log
        self.shard_dir = out_dir / "shards"
        self.shard_dir.mkdir(parents=True, exist_ok=True)
        self._children: set[subprocess.Popen[bytes]] = set()
        self._lock = threading.Lock()
        self._serial = 0
        self._token = secrets.token_hex(1)  # two runs on one machine never share a shard directory
        self.epoch = time.monotonic()
        self.child_environment: dict[str, str] = {}  # TT_RUNTIME_HOST and its log, when the run has one host
        self.stopped = threading.Event()  # set by SIGINT, SIGTERM or SIGHUP: start nothing more

    def argv(self, shard: Shard, out: Path) -> list[str]:
        argv = [
            sys.executable, "-m", "zerg.qa.terminal_truth", "run", "--scenario", shard.scenario, "--cold-cell", "none",
            "--engine", str(shard.engine), "--longhouse-cli", str(shard.cli), "--keep", "--out", str(out),
        ]  # fmt: skip
        for unit in shard.units:
            argv += ["--cell", unit.cell]
        return argv + shard.flags

    def run(self, shard: Shard, attempt: str = "") -> ShardRun:
        with self._lock:
            self._serial += 1
            serial = self._serial
        name = f"{shard.name}{attempt}"
        out, log_path = self.shard_dir / f"{name}.json", self.shard_dir / f"{name}.log"
        scratch = self.scratch_base / f"{self._token}{serial:03d}"  # short: provider sockets live under it
        if self.stopped.is_set():
            note = "the run was stopped before this shard started"
            return ShardRun(
                shard,
                self._rows(shard, out, None, note, log_path),
                None,
                0.0,
                time.monotonic() - self.epoch,
                scratch,
                log_path,
                note,
                keep=False,
            )
        scratch.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)
        environment = {**os.environ, "TT_SCRATCH_ROOT": str(scratch), **self.child_environment}
        started = time.monotonic()
        note = ""
        with log_path.open("wb") as log_file:
            child = subprocess.Popen(
                self.argv(shard, out), cwd=SERVER_ROOT, env=environment, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True
            )
            with self._lock:
                self._children.add(child)
            try:
                code: int | None = child.wait(timeout=SHARD_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                note, code = f"the shard was killed after {SHARD_TIMEOUT_S:.0f} s", None
                _kill_group(child)
            finally:
                with self._lock:
                    self._children.discard(child)
        seconds = time.monotonic() - started
        # What a killed or crashed shard left running would hold its scratch and skew the cells still running.
        leaked = leaked_processes(scratch)
        if leaked:
            note = (note + "; " if note else "") + f"{len(leaked)} process(es) outlived the shard and were killed"
        rows = self._rows(shard, out, code, note, log_path)
        self.log(f"{name}: exit {code} in {seconds:.0f} s ({', '.join(u.cell for u in shard.units)})")
        run = ShardRun(shard, rows, code, seconds, started - self.epoch, scratch, log_path, note)
        if shard.embedding:
            run.embedding_loaded = _host_log_has(scratch, EMBEDDING_LOADED)
        return run

    def _rows(self, shard: Shard, out: Path, code: int | None, note: str, log_path: Path) -> dict[str, dict[str, Any]]:
        found: list[dict[str, Any]] = []
        with contextlib.suppress(OSError, json.JSONDecodeError):
            found = json.loads(out.read_text())
        by_cell = {str(row.get("cell") or f"{row.get('provider')}/{row.get('mode')}"): row for row in found}
        rows: dict[str, dict[str, Any]] = {}
        for unit in shard.units:
            row = by_cell.get(unit.cell)
            if row is None:
                tail = " ".join(_tail(log_path).split())[-400:]
                reason = f"the shard left no result for this cell (exit {code}{'; ' + note if note else ''}): {tail}"
                provider, mode = unit.cell.split("/")
                row = {
                    "cell": unit.cell, "provider": provider, "mode": mode, "scenario": unit.scenario, "status": "could_not_run",
                    "outcome": "fail", "first_failed_stage": "preflight", "reason": reason, "checks": [],
                    "stages": {"preflight": {"ok": False, "reason": reason, "evidence": {}}}, "lanes": [],
                }  # fmt: skip
            rows[unit.id] = row
        return rows

    def kill_all(self) -> None:
        with self._lock:
            children = list(self._children)
        for child in children:
            _kill_group(child)

    def stop(self) -> None:
        """Start nothing more and end what is running. A stopped run still writes its files and fails."""

        self.stopped.set()
        self.kill_all()


def _host_log_has(scratch: Path, text: str) -> bool:
    needle = text.encode()
    for log_path in scratch.glob("*/runtime-host.log"):
        with contextlib.suppress(OSError):
            if needle in log_path.read_bytes():
                return True
    return False


def _log_has(log_path: Path, text: str) -> bool:
    with contextlib.suppress(OSError):
        return text.encode() in log_path.read_bytes()
    return False


def short_scratch(root: Path, tmp: Path = Path("/tmp")) -> tuple[Path, Path | None]:
    """(the scratch root the shards are given, the symlink made for it or None).

    A root that leaves a provider home longer than a socket path allows gets a short symlink in /tmp named by a hash of
    the root, so two runs with different roots never share one. The caller removes the symlink; the files stay in the root.
    """

    if len(str(root)) <= SCRATCH_ROOT_MAX:
        return root, None
    alias = tmp / f"tt{hashlib.sha256(str(root).encode()).hexdigest()[:4]}"
    if len(str(alias)) > SCRATCH_ROOT_MAX:
        raise SystemExit(f"no scratch path short enough: {alias} is over {SCRATCH_ROOT_MAX} bytes (Codex's socket path limit)")
    root.mkdir(parents=True, exist_ok=True)
    if alias.is_symlink():
        alias.unlink()
    alias.symlink_to(root, target_is_directory=True)
    return alias, alias


def start_shared_host(root: Path, embed_model_dir: Path | None, *, embedding: bool = True) -> ScratchRuntimeHost:
    """One Runtime Host for the whole run (--shared-host), with the embedding lane on as a real host has it."""

    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    environment = {"LONGHOUSE_EMBEDDING_PROJECTOR_DISABLED": "0" if embedding else "1"}
    if embed_model_dir is not None:
        environment["LONGHOUSE_EMBED_MODEL_DIR"] = str(embed_model_dir)
    host = ScratchRuntimeHost(root, environment)
    host.start()
    return host


def _memory_in_use() -> int | None:
    """Bytes of this cgroup's memory that cannot be given back (cgroup v2): what ``memory_room_gib`` counts as used."""

    with contextlib.suppress(OSError, ValueError, KeyError):
        stat = dict(line.split() for line in Path("/sys/fs/cgroup/memory.stat").read_text().splitlines())
        return int(stat["anon"]) + int(stat["shmem"])
    return None


class MemoryWatch(threading.Thread):
    """The most memory in use while the shards ran, for the record: the number ``SHARD_MEMORY_GIB`` is checked against."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="memory-watch")
        self.before, self.peak = _memory_in_use(), 0
        self._done = threading.Event()

    def run(self) -> None:
        while self.before is not None and not self._done.wait(2.0):
            self.peak = max(self.peak, _memory_in_use() or 0)

    def finish(self) -> dict[str, float] | None:
        self._done.set()
        if self.before is None or not self.peak:
            return None
        return {"before_gib": round(self.before / 2**30, 2), "peak_gib": round(self.peak / 2**30, 2)}


def memory_room_gib() -> tuple[float, str] | None:
    """Memory this process tree may still take, and where the number came from. None when it cannot be read."""

    with contextlib.suppress(OSError, ValueError):
        limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        used = _memory_in_use()  # file cache is given back under pressure; anonymous and shared memory are not
        if limit != "max" and used is not None:
            return (int(limit) - used) / 2**30, f"a limit of {int(limit) / 2**30:.1f} GiB with {used / 2**30:.1f} GiB in use"
    with contextlib.suppress(OSError, ValueError, StopIteration):
        line = next(row for row in Path("/proc/meminfo").read_text().splitlines() if row.startswith("MemAvailable:"))
        available = int(line.split()[1]) / 2**20
        return available, f"{available:.1f} GiB available on the machine"
    return None


def fit_jobs(
    requested: int, room: tuple[float, str] | None, per_shard: float = SHARD_MEMORY_GIB, set_aside: float = RESERVED_MEMORY_GIB
) -> tuple[int, str]:
    """Shards to run at once: what was asked for, or fewer when memory holds fewer. Never fewer than one.

    2026-11-13: eight shards at 2.3 GiB each ran on a shared server that was already short of memory, and the server
    stalled. A run now takes only what its limit holds.
    """

    if room is None:
        return max(1, requested), ""
    gib, source = room
    fits = max(1, int((gib - set_aside) / per_shard))
    if fits >= requested:
        return max(1, requested), ""
    return fits, f"{fits} at a time, not {requested}: {source}, and a shard is budgeted {per_shard} GiB"


def _kill_group(child: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(child.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        child.wait(timeout=10)


def _tail(path: Path, size: int = 2000) -> str:
    with contextlib.suppress(OSError):
        return path.read_bytes()[-size:].decode("utf-8", "replace")
    return ""


def verdict_of(unit: Unit, row: dict[str, Any]) -> tuple[str, str | None, str]:
    """(outcome, first failed stage, reason) of one cell run, by the serial command's own rules."""

    if unit.kinds:
        return kinds_verdict(row)
    return str(row.get("outcome") or "fail"), row.get("first_failed_stage"), str(row.get("reason") or "").strip()


def gate_outcome(unit: Unit, verdict: tuple[str, str | None, str]) -> str:
    """pass | fail | finding for an unmutated cell.

    A finding is a late served state on a cell where that is declared known (KNOWN_LATE_STATE): it is reported and does
    not fail the run. On every other cell a late state is a failure.
    """

    outcome, stage, _reason = verdict
    if unit.kinds:
        if outcome == "fail" and stage == "state_shown" and unit.cell in KNOWN_LATE_STATE:
            return "finding"
        return "pass" if outcome == "pass" else "fail"
    return "pass" if outcome in GOOD else "fail"


def as_wanted(unit: Unit, mutation: mutations.Mutation | None, verdict: tuple[str, str | None, str]) -> bool:
    """Did this one run come out the way its check needs, judged on its own (the other half of a mutation test aside)."""

    outcome, stage, _reason = verdict
    if unit.extra:
        return bool(extra_row(unit.extra, verdict)["ok"])
    if mutation is not None and unit.mutation:
        return outcome == "fail" and stage in mutation.stages
    return gate_outcome(unit, verdict) != "fail"


def mutation_row(mutation: mutations.Mutation, base: tuple[str, str | None, str], mutated: tuple[str, str | None, str]) -> dict[str, Any]:
    """One row of the mutation table from the two cells' verdicts (the serial command's rule, both halves already run)."""

    outcome, stage, reason, finding = mutations.judge_unmutated(mutation, base)
    row: dict[str, Any] = {
        "name": mutation.name,
        "check": f"mutation {mutation.name}",
        "defect": mutation.defect,
        "cell": mutation.cell if not mutation.scenario else f"{mutation.cell} ({mutation.scenario})",
        "expected_stage": "/".join(mutation.stages),
        "baseline": outcome,
    }
    if finding:
        row["state_finding"] = finding
    observed, observed_stage, observed_reason = mutated
    row.update(observed_outcome=observed, observed_stage=observed_stage, reason=observed_reason)
    if outcome != "pass":
        # The row is a miss whatever the mutated cell did: a gate that fails the unmutated cell proves nothing by failing the other.
        row.update(baseline_stage=stage, baseline_reason=reason, ok=False)
    else:
        row["ok"] = observed == "fail" and observed_stage in mutation.stages
    return row


def extra_row(name: str, verdict: tuple[str, str | None, str]) -> dict[str, Any]:
    label, cell, _flags, want = EXTRA_CHECKS[name]
    outcome, stage, reason = verdict
    ok = outcome == "pass" if want == "(pass)" else (outcome == "fail" and stage == want)
    return {
        "name": name, "check": label, "cell": cell, "expected_stage": want, "observed_outcome": outcome, "observed_stage": stage,
        "reason": reason, "ok": ok,
    }  # fmt: skip


@dataclass
class Judged:
    cells: dict[str, dict[str, Any]]  # unmutated unit id -> {outcome, stage, reason}
    mutation_rows: list[dict[str, Any]]
    wrong: list[str]  # ids of the units to run again: failing unmutated cells, and mutated cells that came out wrong

    @property
    def ok(self) -> bool:
        return all(c["outcome"] != "fail" for c in self.cells.values()) and all(r["ok"] for r in self.mutation_rows)


def judge(plan: Plan, rows: dict[str, dict[str, Any]]) -> Judged:
    verdicts = {unit.id: verdict_of(unit, rows[unit.id]) for unit in [*plan.units.values(), *plan.mutated.values(), *plan.extras.values()]}
    cells: dict[str, dict[str, Any]] = {}
    wrong: list[str] = []
    for unit in plan.units.values():
        outcome, stage, reason = verdicts[unit.id]
        gate = gate_outcome(unit, verdicts[unit.id])
        cells[unit.id] = {
            "id": unit.id,
            "cell": unit.cell,
            "scenario": unit.scenario,
            "outcome": gate,
            "verdict": outcome,
            "stage": stage,
            "reason": reason,
        }
        if gate == "fail":
            wrong.append(unit.id)
    table: list[dict[str, Any]] = []
    for m in plan.selected:
        row = mutation_row(m, verdicts[plan.baseline_of[m.name]], verdicts[plan.mutated[m.name].id])
        table.append(row)
        if not row["ok"]:
            if row["baseline"] != "pass":
                wrong.append(plan.baseline_of[m.name])
            if not as_wanted(plan.mutated[m.name], m, verdicts[plan.mutated[m.name].id]):
                wrong.append(plan.mutated[m.name].id)
    for name, unit in plan.extras.items():
        row = extra_row(name, verdicts[unit.id])
        table.append(row)
        if not row["ok"]:
            wrong.append(unit.id)
    return Judged(cells, table, list(dict.fromkeys(wrong)))


def _median(row: dict[str, Any]) -> float | None:
    for lane in row.get("lanes") or []:
        if "SSE" in str(lane.get("lane", "")):
            return lane.get("median_delay_s")
    return None


def render(plan: Plan, judged: Judged, rows: dict[str, dict[str, Any]], reruns: Sequence[dict[str, Any]]) -> str:
    lines = ["### Cells", "", "| cell | scenario | verdict | stage | live median | reason |", "|---|---|---|---|---|---|"]
    for item in sorted(judged.cells.values(), key=lambda c: (ALL_SCENARIOS.index(c["scenario"]), c["id"])):
        unit = plan.units[item["id"]]
        median = _median(rows[unit.id]) if not unit.kinds else None
        shown = item["outcome"] if item["outcome"] != "pass" or item["verdict"] == "pass" else item["verdict"]
        reason = "" if item["outcome"] == "pass" else " ".join(str(item["reason"]).split())[:240]
        label = unit.cell + (f" ({unit.options.suffix()})" if unit.options != standard_options(unit.cell, unit.scenario) else "")
        lines.append(
            f"| {label} | {unit.scenario} | {shown} | {item['stage'] or '-'} | {'-' if median is None else f'{median:.2f}s'} | {reason} |"
        )
    if judged.mutation_rows:
        lines += ["", "### Mutation tests", "", *mutations.render_rows(judged.mutation_rows)]
    if reruns:
        lines += ["", "### Run again alone", ""]
        lines += [f"- {r['id']}: first {r['first']}, alone {r['again']}" for r in reruns]
    return "\n".join(lines)


def run_all(args: argparse.Namespace) -> int:
    def log(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    scenarios = args.scenario or list(ALL_SCENARIOS)
    plan = make_plan(scenarios=scenarios, providers=args.provider, modes=args.mode, with_mutations=args.mutations, only=args.only)
    engine, cli = args.engine.resolve(), args.longhouse_cli.resolve()
    builds: dict[str, Any] = {}
    mutated_engines: dict[str, tuple[Path, Path]] = {}
    server_copies: dict[str, Path] = {}
    copies_root = SCRATCH_ROOT / "server-copies"
    if plan.selected:
        mutated_engines, builds = mutations.build_mutated_engines(
            plan.selected, cache=args.mutation_cache, cargo_target=args.cargo_target, log=log
        )
        log(f"mutated engines: {builds['hits']} from the cache, {builds['built']} built, {builds['seconds']} s")
        for m in plan.selected:
            if m.side == "server":
                server_copies[m.name] = mutations.prepare_server_copy(m, copies_root / m.name)
    shards = make_shards(
        plan, engine=engine, cli=cli, providers_per_shard=args.providers_per_shard, tokens=args.tokens, rate=args.rate,
        mutated_engines=mutated_engines, server_copies=server_copies, embed_model_dir=args.embed_model_cache,
    )  # fmt: skip
    args.jobs_asked = args.jobs
    budget = (
        (SHARED_HOST_SHARD_MEMORY_GIB, SHARED_HOST_RESERVED_MEMORY_GIB) if args.shared_host else (SHARD_MEMORY_GIB, RESERVED_MEMORY_GIB)
    )
    args.jobs, why = fit_jobs(args.jobs, memory_room_gib(), *budget)
    if why:
        log(why)
    scratch_root, alias = short_scratch(SCRATCH_ROOT)
    if alias is not None:
        log(f"scratch is {alias}, a link to {SCRATCH_ROOT}: provider socket paths have to fit in {SOCKET_PATH_MAX} bytes")
    runner = Runner(out_dir, scratch_root, log)
    shared_host: ScratchRuntimeHost | None = None
    args.shared_host_state = None
    watch = MemoryWatch()
    watch.start()
    by_name = {m.name: m for m in plan.selected}

    def run_shard(shard: Shard, attempt: str = "") -> ShardRun:
        """Run it, and keep its scratch (recordings, host and engine logs) only if a row did not come out as wanted."""

        run = runner.run(shard, attempt)
        fine = all(as_wanted(u, by_name.get(u.mutation or ""), verdict_of(u, run.rows[u.id])) for u in shard.units)
        run.keep = (not fine or run.exit is None) and run.scratch.exists()
        if not run.keep:
            shutil.rmtree(run.scratch, ignore_errors=True)
        return run

    log(f"{sum(len(s.units) for s in shards)} cell runs in {len(shards)} shards, {args.jobs} at a time")
    runs: list[ShardRun] = []
    rows: dict[str, dict[str, Any]] = {}
    reruns: list[dict[str, Any]] = []

    # A run whose terminal or SSH session went away must not keep 8 hosts and their providers running unattended
    # (2026-11-13: a dropped connection left a run going on a machine that was out of memory).
    def on_signal(number: int, _frame: Any) -> None:
        log(f"signal {number}: stopping; the shards that did not finish are failures")
        runner.stop()

    previous_handlers = {}
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous_handlers[number] = signal.signal(number, on_signal)
    try:
        if args.shared_host:
            boot = time.monotonic()
            shared_host = start_shared_host(scratch_root / "shared-host", args.embed_model_cache, embedding=args.embedding_lane)
            runner.child_environment = {"TT_RUNTIME_HOST": shared_host.url, "TT_RUNTIME_HOST_LOG": str(shared_host.log_path)}
            log(f"one Runtime Host for the run, up in {time.monotonic() - boot:.1f} s at {shared_host.url}")
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            runs = list(pool.map(run_shard, sorted(shards, key=lambda s: -s.weight)))
        for run in runs:
            rows.update(run.rows)
        parallel_s = time.monotonic() - started
        judged = judge(plan, rows)
        first = {uid: verdict_of(unit, rows[uid]) for uid, unit in _all_units(plan).items()}
        if runner.stopped.is_set():
            pass  # nothing is run again after a stop
        elif judged.wrong and args.rerun and len(judged.wrong) <= MAX_RERUNS:
            log(f"running {len(judged.wrong)} again, one at a time: {', '.join(judged.wrong)}")
            shard_of = {unit.id: shard for shard in shards for unit in shard.units}
            for uid in judged.wrong:
                unit = _all_units(plan)[uid]
                home = shard_of[uid]
                alone = Shard(_safe(uid), unit.scenario, [unit], home.engine, home.cli, home.flags)
                again = run_shard(alone, attempt=".again")
                runs.append(again)
                rows[uid] = {**again.rows[uid], "first_try": _brief(first[uid])}
                reruns.append({"id": uid, "first": _brief(first[uid]), "again": _brief(verdict_of(unit, again.rows[uid]))})
            judged = judge(plan, rows)
        elif judged.wrong and args.rerun:
            log(f"{len(judged.wrong)} rows came out wrong, more than {MAX_RERUNS}: none is run again")
    finally:
        args.memory = watch.finish()
        runner.kill_all()
        if shared_host is not None:
            args.shared_host_state = {
                "exited_during_the_run": shared_host.process.poll() if shared_host.process is not None else None,
                "embedding_model_loaded": _log_has(shared_host.log_path, EMBEDDING_LOADED),
                "root": str(SCRATCH_ROOT / "shared-host"),
            }
            shared_host.stop()
        if alias is not None:
            with contextlib.suppress(OSError):
                alias.unlink()
        shutil.rmtree(copies_root, ignore_errors=True)
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)
    args.stopped = runner.stopped.is_set()
    host_exit = (args.shared_host_state or {}).get("exited_during_the_run")
    if host_exit is not None:
        log(f"the run's Runtime Host exited before the run ended (status {host_exit}): the run fails")
    args.ok = judged.ok and host_exit is None
    kept = _write(args, plan, judged, rows, runs, reruns, builds, shards, parallel_s, time.monotonic() - started)
    if args.shared_host_state and args.shared_host_state["root"] not in kept:
        shutil.rmtree(args.shared_host_state["root"], ignore_errors=True)
    print(render(plan, judged, rows, reruns))
    wall = time.monotonic() - started
    print(
        f"\nterminal truth, all checks: {'PASS' if args.ok else 'FAIL'} "
        f"({len(plan.units)} cells, {len(judged.mutation_rows)} mutation checks, {len(shards)} shards {args.jobs} at a time, "
        f"{wall:.0f} s; one after another the shards are {sum(r.seconds for r in runs):.0f} s; {len(kept)} scratch kept)"
    )
    return 0 if args.ok else 1


def _all_units(plan: Plan) -> dict[str, Unit]:
    return {u.id: u for u in [*plan.units.values(), *plan.mutated.values(), *plan.extras.values()]}


def _brief(verdict: tuple[str, str | None, str]) -> str:
    outcome, stage, reason = verdict
    return f"{outcome} at {stage or '-'}: {' '.join(reason.split())[:300]}" if outcome != "pass" else "pass"


def _write(
    args: argparse.Namespace,
    plan: Plan,
    judged: Judged,
    rows: dict[str, dict[str, Any]],
    runs: Sequence[ShardRun],
    reruns: Sequence[dict[str, Any]],
    builds: dict[str, Any],
    shards: Sequence[Shard],
    parallel_s: float,
    wall_s: float,
) -> list[str]:
    """Write the result files; returns the scratch directories still kept (shards with a row that came out wrong)."""

    out_dir: Path = args.out_dir
    for scenario in REPLY_SCENARIOS:
        members = [u for u in plan.units.values() if u.scenario == scenario and u.options == standard_options(u.cell, scenario)]
        if members:
            (out_dir / f"pass-{args.pass_index}-{scenario}.json").write_text(json.dumps([rows[u.id] for u in members], indent=2) + "\n")
    for scenario in KIND_SCENARIOS:
        members = [u for u in plan.units.values() if u.scenario == scenario]
        if members:
            (out_dir / f"kinds-{scenario}.json").write_text(json.dumps([rows[u.id] for u in members], indent=2) + "\n")
    (out_dir / "kinds.json").write_text(json.dumps([c for c in judged.cells.values() if c["scenario"] in KIND_SCENARIOS], indent=2) + "\n")
    table = []
    for row in judged.mutation_rows:
        unit = next((u for u in [*plan.mutated.values(), *plan.extras.values()] if _check_name(u) == row["check"]), None)
        if not row["ok"] and unit is not None:
            keys = mutations.EVIDENCE_KEYS if unit.kinds else mutations.REPLY_EVIDENCE_KEYS
            failing = plan.baseline_of.get(unit.mutation or "", unit.id) if row.get("baseline", "pass") != "pass" else unit.id
            row = {**row, "evidence": {k: rows[failing][k] for k in keys if k in rows[failing]}}
        table.append(row)
    if table:
        (out_dir / "mutations.json").write_text(json.dumps(table, indent=2) + "\n")
    for run in runs:
        if not run.keep:  # a provider process on its way out can write into its HOME after the first removal
            shutil.rmtree(SCRATCH_ROOT / run.scratch.name, ignore_errors=True)
    # By the scratch root's own name: a short link the run made for it (``short_scratch``) is gone by now.
    kept = [str(SCRATCH_ROOT / run.scratch.name) for run in runs if run.keep]
    host = getattr(args, "shared_host_state", None)
    if host and (kept or host["exited_during_the_run"] is not None):
        kept.append(host["root"])  # the host's log is the other half of every kept shard's evidence
    fleet = {
        "state": "success" if getattr(args, "ok", judged.ok) else "failure",
        # False only for the whole gate: every scenario, provider and mode, every mutation test, the gate's stream.
        "stopped": getattr(args, "stopped", False),
        "narrowed": bool(
            args.scenario
            or args.provider
            or args.mode
            or args.only
            or not args.mutations
            or (args.tokens, args.rate) != (100, 20.0)
            or getattr(args, "shared_host", False)
        ),
        "reruns_allowed": bool(args.rerun),
        "jobs": args.jobs,
        "jobs_asked": getattr(args, "jobs_asked", args.jobs),
        "providers_per_shard": args.providers_per_shard,
        # The shard whose host ran the embedding lane, and whether the model loaded while its cells ran.
        "embedding_lane": next(
            (
                {"shard": run.log.stem, "loaded": bool(run.embedding_loaded)}
                for run in runs
                if run.shard.embedding and not run.log.stem.endswith(".again")
            ),
            None,
        ),
        # With --shared-host: whether the run's one host was still up at the end and whether its embedding model loaded.
        "shared_host": host,
        # Memory in use (anonymous and shared, as fit_jobs counts it) before the shards started and at its highest.
        "memory": getattr(args, "memory", None),
        "wall_s": round(wall_s, 1),
        "parallel_phase_s": round(parallel_s, 1),
        "one_after_another_s": round(sum(r.seconds for r in runs), 1),
        "planned": {
            "cells": sorted(plan.units),
            "mutations": [m.name for m in plan.selected] + list(plan.extras),
        },
        "cells": list(judged.cells.values()),
        "reruns": list(reruns),
        "mutated_engine_builds": builds,
        "kept_scratch": kept,
        "shards": [
            {
                "name": run.log.stem,
                "cells": [u.id for u in run.shard.units],
                "exit": run.exit,
                "started_s": round(run.started_s, 1),
                "seconds": round(run.seconds, 1),
                "note": run.note,
            }
            for run in runs
        ],
    }
    (out_dir / "fleet.json").write_text(json.dumps(fleet, indent=2) + "\n")
    return kept


def _check_name(unit: Unit) -> str:
    return f"mutation {unit.mutation}" if unit.mutation else EXTRA_CHECKS[unit.extra or ""][0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--longhouse-cli", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 4, 8), help="shards running at once")
    parser.add_argument(
        "--shared-host",
        action="store_true",
        help="one Runtime Host for the whole run instead of one per shard: measures what one host carries; not the gate",
    )
    parser.add_argument(
        "--no-embedding-lane", dest="embedding_lane", action="store_false", help="with --shared-host: that host without the embedding lane"
    )
    parser.add_argument("--providers-per-shard", type=int, default=2, help="providers whose unmutated cells share one host, per scenario")
    parser.add_argument("--scenario", action="append", choices=ALL_SCENARIOS, help="repeatable; default all five")
    parser.add_argument("--provider", action="append", help="repeatable; default every declared provider")
    parser.add_argument("--mode", action="append", choices=["console", "helm"], help="repeatable; default both")
    parser.add_argument("--no-mutations", dest="mutations", action="store_false", help="unmutated cells only")
    parser.add_argument("--only", action="append", help="mutation name, 'reworded-ready-string' or 'half-speed'; repeatable")
    parser.add_argument("--no-rerun", dest="rerun", action="store_false", help="judge the first try only (the nightly's raw rate)")
    parser.add_argument("--mutation-cache", type=Path, default=SCRATCH_ROOT.parent / "tt-mutants", help="mutated engine binaries")
    parser.add_argument("--cargo-target", type=Path, default=REPO_ROOT / ".build" / "cargo-target", help="warm cargo target to clone")
    parser.add_argument(
        "--embed-model-cache",
        type=Path,
        help="where the one shard that runs the embedding lane keeps the model (the host verifies it by checksum and downloads what is missing)",
    )
    parser.add_argument("--tokens", type=int, default=100)
    parser.add_argument("--rate", type=float, default=20.0)
    parser.add_argument("--pass-index", type=int, default=1, help="the <i> of pass-<i>-<scenario>.json")
    args = parser.parse_args(argv)
    if not args.engine.is_file() or not args.longhouse_cli.is_file():
        parser.error("pass --engine and --longhouse-cli (built from this checkout)")
    return run_all(args)


if __name__ == "__main__":
    raise SystemExit(main())
