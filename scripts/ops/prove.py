#!/usr/bin/env python3
"""Run named live checks (cells) against any commit on the Linux proof host and say pass or fail per cell.

    make prove CELLS="claude/helm:tool pi_helm_steer_active" SHA=<commit>        (SHA defaults to HEAD)
    scripts/ops/prove.py --cells "claude/helm codex/console:thinking" --sha origin/main

A cell is one of:
  * a Terminal Truth cell, ``provider/mode`` with an optional ``:scenario`` (long_reply when omitted), for example
    ``claude/helm``, ``claude/helm:tool``, ``codex/console:thinking``. Scenarios: long_reply, two_message, thinking,
    tool, subagent, question, question_steer (Claude Helm only: AskUserQuestion answered from Longhouse), slash_command,
    steer (a steer from Longhouse enters the running turn). Judged by ``zerg.qa.terminal_truth`` exactly as the Terminal Truth gate judges it.
  * a factory producer cell, an assertion id such as ``pi_helm_steer_active``, run by its producer
    (``server/zerg/qa``) against a scratch Runtime Host with the credential-free scripted model. A producer runs
    its whole lifecycle once and every named cell of it is read from that one result.

Output is one line per cell, ``PASS|FAIL <cell> <seconds> <evidence path or reason>``, then a summary line. The exit
status is 1 when any cell fails, cannot be resolved or cannot run, 2 for a usage error.

How it works. This file runs in two places: here (the orchestrator, standard library only) and on the proof host
(``--on-host``, the same file copied over for that run, so an old SHA is judged by today's runner). The proof host is
``cube-proof`` (Linux, ``PROVE_HOST`` overrides it), a disposable container that exists only for these runs; provider
CLIs never run on this Mac, and never in David's workspace (``cube-ws``). On the proof host, per run:

  1. the commit comes from a bare repository there (fetched from origin, or pushed from this checkout when it is
     not on origin yet) and is checked out as a disposable worktree;
  2. the Runtime Host environment is ``uv sync --frozen`` from that commit's lock file into the run's own venv;
  3. the provider CLIs are the exact versions the Terminal Truth CI lane pins (scripts/ci/terminal-truth-providers.toml),
     unpacked from an archive cache (a first install is 20 s);
  4. the engine and CLI are built with the ``ci`` profile, or copied from ``bin-cache/<digest>`` when the engine's
     inputs (engine/, config/, schemas/, scripts/build, the compiler, the version) are the same as an earlier
     build's. Most commits touch none of them, so most runs build nothing; ``by-sha/<sha>`` records which build a
     commit used. A miss builds in one persistent worktree under one lock into one persistent cargo target, so it
     recompiles only what changed;
  5. the cells run, Terminal Truth cells grouped by scenario (one host and engine per group) and producer cells one
     producer at a time. Scratch is on tmpfs (``/dev/shm``), as in the CI lane;
  6. teardown, on success, failure, timeout and signal: every process group the run started is killed, any process
     still carrying the run's marker or living in its scratch is killed, the scratch, venv and worktree are removed,
     and the result line says whether anything was left. Only ``out/`` (results, logs, producer evidence) stays, on
     the proof host for 24 hours and copied here under /tmp/agents/prove/<run>/.

Not built: ``TARGET=factory`` (the factory checking a priority prove request between cells) and ``TARGET=wisp``
(macOS-only cells; no provider cell needs macOS today). Both refuse by name instead of falling back.
Spec: control-plane/docs/specs/multi-agent-dev-loop-speed.md, change 5.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import getpass
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

PROOF_HOST = os.environ.get("PROVE_HOST", "cube-proof")
PROOF_BASE = ".cache/longhouse-prove"  # relative to the proof host user's home
LOCAL_OUT = Path("/tmp/agents/prove")
ORIGIN_URL = "https://github.com/cipher982/longhouse.git"

TT_SCENARIOS = ("long_reply", "two_message", "thinking", "tool", "subagent", "question", "question_steer", "slash_command", "steer")
KIND_SCENARIOS = ("thinking", "tool", "subagent")
TT_PROVIDERS = ("claude", "codex", "opencode", "pi", "omp", "antigravity", "cursor")
# Providers whose cells run a fake CLI from the repo (nothing to install), and the scenarios that have such a cell.
# Cursor's CLI reaches only Cursor's servers, so no scripted model can stand in for it (fake_cursor_agent.py).
TT_FAKE_PROVIDERS = {"cursor": ("slash_command",)}
TT_CELL = re.compile(r"^(?P<provider>[a-z]+)/(?P<mode>helm|console|shadow)(?::(?P<scenario>[a-z_]+))?$")
ASSERTION_CELL = re.compile(r"^[a-z][a-z0-9_]+$")

# Producers whose proof-host envelope has been run end to end against the scripted model. The factory builds each
# producer's argv from its own plan; this is the part of that this runner restates, so a producer is added here only
# after a real run (spec: "what remains"). producer_id -> (provider, model the scripted endpoint answers to).
PROOF_PRODUCERS = {
    "pi.helm_lifecycle.v1": ("pi", "mock-model"),
    "omp.helm_lifecycle.v1": ("omp", "longhouse-mock/mock-model"),
}

CELL_TIMEOUT_S = 420  # one Terminal Truth cell is 20 to 75 s; a hung provider must not hold the proof host
PRODUCER_TIMEOUT_S = 600  # a Pi Helm lifecycle is about 110 s
DEFAULT_DEADLINE_S = 1500
EVIDENCE_KEEP_S = 24 * 3600
MARKER_ENV = "PROVE_RUN_ID"


def log(message: str) -> None:
    print(f"[prove] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------------------------------------------------
# Cell names
# --------------------------------------------------------------------------------------------------------------------


def parse_cells(text: str) -> list[str]:
    cells = [c for c in re.split(r"[\s,]+", text.strip()) if c]
    seen: dict[str, None] = {}
    for cell in cells:
        seen.setdefault(cell, None)
    return list(seen)


def classify(cell: str) -> tuple[str, dict]:
    """("tt", {...}) for a Terminal Truth cell, ("producer", {...}) for an assertion id, or ("bad", {reason})."""

    match = TT_CELL.match(cell)
    if match:
        provider, mode, scenario = match["provider"], match["mode"], match["scenario"] or "long_reply"
        if provider not in TT_PROVIDERS:
            return "bad", {"reason": f"unknown provider {provider!r} (Terminal Truth providers: {', '.join(TT_PROVIDERS)})"}
        if scenario not in TT_SCENARIOS:
            return "bad", {"reason": f"unknown scenario {scenario!r} (scenarios: {', '.join(TT_SCENARIOS)})"}
        if provider in TT_FAKE_PROVIDERS and scenario not in TT_FAKE_PROVIDERS[provider]:
            allowed = ", ".join(TT_FAKE_PROVIDERS[provider])
            return "bad", {"reason": f"{provider} has no scripted model; only {allowed} runs it (against a fake CLI)"}
        return "tt", {"provider": provider, "mode": mode, "scenario": scenario, "key": f"{provider}/{mode}"}
    if ASSERTION_CELL.match(cell):
        return "producer", {"assertion": cell}
    return "bad", {"reason": "not a cell name: use provider/mode[:scenario] (claude/helm:tool) or a producer assertion id (pi_helm_steer_active)"}


# --------------------------------------------------------------------------------------------------------------------
# Orchestrator (this machine)
# --------------------------------------------------------------------------------------------------------------------


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=cwd or repo_root(), capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        raise SystemExit(f"prove: git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def ssh(command: str, *, timeout: int | None = None, stdin=None, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=20", PROOF_HOST, command],
        capture_output=True, text=True, timeout=timeout, stdin=stdin, check=check,
    )  # fmt: skip


def ensure_commit_on_host(sha: str) -> None:
    """Make the commit exist in the proof host's bare repository: fetch it from origin, else push it from this checkout."""

    base = f"$HOME/{PROOF_BASE}"
    probe = ssh(
        f"set -e; mkdir -p {base}; cd {base}; [ -d repo.git ] || git init -q --bare repo.git; "
        f"git -C repo.git remote get-url origin >/dev/null 2>&1 || git -C repo.git remote add origin {ORIGIN_URL}; "
        f"git -C repo.git cat-file -e {sha}^{{commit}}",
        timeout=60,
    )  # fmt: skip
    if probe.returncode == 0:
        return
    fetched = ssh(f"git -C {base}/repo.git fetch -q --depth=1 origin {sha}", timeout=120)
    if fetched.returncode == 0:
        return
    log(f"{sha[:12]} is not on origin; pushing it from this checkout")
    pushed = subprocess.run(
        ["git", "push", "-q", f"{PROOF_HOST}:{PROOF_BASE}/repo.git", f"{sha}:refs/prove/{sha}"],
        cwd=repo_root(), capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    if pushed.returncode != 0:
        raise SystemExit(f"prove: cannot get {sha[:12]} to {PROOF_HOST}: fetch said {fetched.stderr.strip()!r}, push said {pushed.stderr.strip()!r}")


def fetch_results(run_id: str) -> Path:
    dest = LOCAL_OUT / run_id
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["rsync", "-a", "--exclude", "scratch-copy/**/node_modules", f"{PROOF_HOST}:{PROOF_BASE}/runs/{run_id}/out/", f"{dest}/"],
        capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    return dest


def orchestrate(args: argparse.Namespace) -> int:
    started = time.monotonic()
    cells = parse_cells(args.cells)
    if not cells:
        print("prove: no cells (CELLS=\"claude/helm pi_helm_steer_active\")", file=sys.stderr)
        return 2
    if args.target != "proof":
        print(
            f"prove: TARGET={args.target} is not built. `proof` runs on {PROOF_HOST} (Linux, scripted model); the factory "
            "priority request and a macOS target are recorded as next in control-plane/docs/specs/multi-agent-dev-loop-speed.md.",
            file=sys.stderr,
        )
        return 2
    if args.fault and (len(cells) != 1 or classify(cells[0])[0] != "producer"):
        print("prove: FAULT=<name> runs one producer cell (CELLS=\"pi_helm_send_idle\") as a negative control", file=sys.stderr)
        return 2
    bad = {c: classify(c)[1]["reason"] for c in cells if classify(c)[0] == "bad"}
    if bad:
        for cell, reason in bad.items():
            print(f"FAIL {cell}  unusable: {reason}")
        return 1 if len(bad) == len(cells) else 2
    sha = git("rev-parse", "--verify", "--quiet", f"{args.sha}^{{commit}}", check=False)
    if not sha:
        print(f"prove: {args.sha!r} is not a commit in this checkout (git fetch first)", file=sys.stderr)
        return 2
    run_id = f"{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}-{sha[:8]}-{secrets.token_hex(2)}"
    log(f"run {run_id}: {len(cells)} cell(s) at {sha} on {PROOF_HOST}")
    tools = f"{PROOF_BASE}/tools/prove-{run_id}.py"
    child: subprocess.Popen | None = None

    def stop_remote(*_: object) -> None:
        log("interrupted; telling the proof host to tear the run down")
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", PROOF_HOST, f"pkill -TERM -f 'prove-{run_id}.py --on-host' || true"],
            capture_output=True, timeout=30, check=False,
        )  # fmt: skip
        if child is not None:
            with contextlib.suppress(Exception):
                child.wait(timeout=90)
        raise SystemExit(130)

    signal.signal(signal.SIGINT, stop_remote)
    signal.signal(signal.SIGTERM, stop_remote)
    try:
        ensure_commit_on_host(sha)
        shipped = ssh(f"mkdir -p $HOME/{PROOF_BASE}/tools && cat > $HOME/{tools}", stdin=open(Path(__file__), "rb"), timeout=60)
        if shipped.returncode != 0:
            raise SystemExit(f"prove: cannot copy the runner to {PROOF_HOST}: {shipped.stderr.strip()}")
        remote = (
            f"timeout -k 30 {int(args.deadline)} python3 $HOME/{tools} --on-host --run-id {run_id} --sha {sha} "
            f"--cells {shlex.quote(' '.join(cells))} --deadline {int(args.deadline)}"
            + (" --keep" if args.keep else "")
            + (f" --fault {shlex.quote(args.fault)}" if args.fault else "")
            + (f" --cell-timeout {int(args.cell_timeout)}" if args.cell_timeout else "")
        )
        child = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=20", PROOF_HOST, remote],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=sys.stderr, text=True,
        )  # fmt: skip
        assert child.stdout is not None
        verdict_json = ""
        for line in child.stdout:
            if line.startswith("VERDICTS "):
                verdict_json = line[len("VERDICTS "):]
        status = child.wait()
        local = fetch_results(run_id)
        if not verdict_json:
            for cell in cells:
                print(f"FAIL {cell}  no verdict: the proof host run died (exit {status}); log {local}/run.log")
            return 1
        report = json.loads(verdict_json)
    finally:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    failed = 0
    for row in report["cells"]:
        evidence = f"{local}/{row['evidence']}" if row.get("evidence") else f"{local}/run.log"
        failed += row["verdict"] != "PASS"
        detail = f"{row['reason']}  " if row.get("reason") else ""
        print(f"{row['verdict']} {row['cell']}  {row['seconds']:.0f}s  {detail}{evidence}")
    cleanup = report.get("cleanup", {})
    phases = " ".join(f"{k}={v:.0f}s" for k, v in report.get("phases", {}).items())
    print(
        f"prove: {len(report['cells']) - failed} pass, {failed} fail; sha {sha[:12]}; engine {report.get('engine', '?')}; "
        f"total {time.monotonic() - started:.0f}s ({phases}); cleanup {cleanup.get('state', '?')}; proof-host evidence {PROOF_HOST}:{PROOF_BASE}/runs/{run_id}/out"
    )  # fmt: skip
    if cleanup.get("state") != "clean":
        failed += 1
        print(f"FAIL cleanup  {cleanup.get('detail', 'unknown')}")
    return 1 if failed or status != 0 else 0


# --------------------------------------------------------------------------------------------------------------------
# Proof-host side
# --------------------------------------------------------------------------------------------------------------------


def fault_wrapper(argv: list[str], env: dict[str, str], fault: str, receipts: Path, timeout: float) -> list[str]:
    """`argv` run as this user inside a private mount namespace where /run/longhouse-qa holds the fault selection.

    This is the factory's engine-only channel (provider_factory/sandbox.py binds the same path): the QA engine reads
    /run/longhouse-qa/fault.json, the producer's argv and environment are exactly an honest run's, and the namespace
    keeps another agent's QA engine on this proof host from reading the selection. Receipts are copied out before exit.
    The inner `timeout` is the cap: the sudo parent is root's and a plain group kill cannot reach it.
    """

    config = json.dumps({"fault": fault, "receipt": "/run/longhouse-qa/receipts.jsonl"})
    script = (
        "set -e; mkdir -p /run/longhouse-qa; mount -t tmpfs -o mode=0755 tmpfs /run/longhouse-qa; "
        "printf '%s' \"$1\" > /run/longhouse-qa/fault.json; touch /run/longhouse-qa/receipts.jsonl; chown \"$2\" /run/longhouse-qa/receipts.jsonl; "
        "shift 2; set +e; runuser -u \"$USER_NAME\" -- \"$@\"; rc=$?; "
        "cp /run/longhouse-qa/receipts.jsonl \"$RECEIPTS\" 2>/dev/null; chown \"$USER_NAME\" \"$RECEIPTS\" 2>/dev/null; exit $rc"
    )
    user = getpass.getuser()
    inner = ["timeout", "-k", "20", str(int(timeout)), "env", "-i", *[f"{k}={v}" for k, v in env.items()], *argv]
    return [
        "sudo", "-n", "env", f"USER_NAME={user}", f"RECEIPTS={receipts}", "unshare", "-m", "--propagation", "private", "--",
        "bash", "-c", script, "_", config, user, *inner,
    ]  # fmt: skip


def control_verdict(result: dict, receipts: Path, target: str, fault: str, seconds: float, evidence: str) -> dict:
    """One negative control read off an ordinary producer result and the engine's receipts, by the factory's rule
    (provider_factory.negative_controls.blind_verdict, control-plane repo): the fault fired, no assertion but the
    target is false, and the target is false and not excused as the model's. PASS means the oracle caught the fault."""

    assertions = result.get("assertions") if isinstance(result.get("assertions"), dict) else {}
    fired = 0
    with contextlib.suppress(OSError):
        fired = sum(1 for line in receipts.read_text().splitlines() if line.strip().startswith("{"))
    others = sorted(name for name, value in assertions.items() if name != target and value is False)
    observation = result.get("observation") if isinstance(result.get("observation"), dict) else {}
    excused = isinstance(observation.get("model_noncompliance"), dict) and target in observation["model_noncompliance"]
    value = assertions.get(target)
    facts = f"fault {fault}: receipts={fired} target={value} other_false={others}"
    if not fired:
        verdict, reason = "FAIL", f"inconclusive, the fault did not fire ({facts})"
    elif type(value) is not bool:
        verdict, reason = "FAIL", f"inconclusive, target not reported ({facts}; {result.get('failure_code') or result.get('status')})"
    elif others:
        verdict, reason = "FAIL", f"inconclusive, preconditions failed ({facts})"
    elif value is True or excused:
        verdict, reason = "FAIL", f"ORACLE BLIND: the producer {'put it down to the model' if excused else 'passed the target under a fired fault'} ({facts})"
    else:
        verdict, reason = "PASS", f"control caught the fault ({facts})"
    return {"verdict": verdict, "reason": reason, "seconds": seconds, "evidence": evidence}


class ProofRun:
    def __init__(self, run_id: str, sha: str, keep: bool, deadline: int, cell_timeout: int = 0, fault: str = "") -> None:
        self.run_id, self.sha, self.keep = run_id, sha, keep
        self.fault = fault
        self.base = Path.home() / PROOF_BASE
        self.repo = self.base / "repo.git"
        self.run_dir = self.base / "runs" / run_id
        self.out = self.run_dir / "out"
        self.tree = self.base / "trees" / f"run-{run_id}"
        self.venv = self.run_dir / "venv"
        # Short on purpose: Codex binds <home>/.longhouse/managed-local/codex-bridge/<uuid>.sock, and a Linux socket path
        # holds 107 bytes. With TT's <root>/tt/<pass>/h home this name leaves exactly that ("/dev/shm/prove-xxxxxx" was 4
        # over, so every Codex Helm cell failed with "path must be shorter than SUN_LEN").
        self.scratch = Path("/dev/shm") / f"lp{secrets.token_hex(3)}"
        self.cell_timeout = cell_timeout
        self.started = time.monotonic()
        self.deadline_at = self.started + deadline - 60  # leave time to tear down before `timeout` fires
        self.phases: dict[str, float] = {}
        self.groups: list[subprocess.Popen] = []
        self.engine_note = "?"
        self.env_base = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            MARKER_ENV: run_id,
        }

    # -- plumbing
    def phase(self, name: str):
        outer = self

        class _Phase:
            def __enter__(self_inner):
                self_inner.t = time.monotonic()

            def __exit__(self_inner, *exc):
                outer.phases[name] = time.monotonic() - self_inner.t
                log(f"{name}: {outer.phases[name]:.1f}s")

        return _Phase()

    def remaining(self) -> float:
        return self.deadline_at - time.monotonic()

    def child_env(self, **extra: str) -> dict[str, str]:
        env = {**self.env_base, "HOME": str(self.scratch / "home"), "TT_SCRATCH_ROOT": str(self.scratch / "tt"), **extra}
        return env

    def run(self, argv: list[str], *, cwd: Path, env: dict[str, str], log_path: Path, timeout: float, label: str) -> int:
        """Run a child in its own process group, with a cap. Returns its exit status, or 124 on timeout."""

        timeout = max(5.0, min(timeout, self.remaining()))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as sink:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT, start_new_session=True)
            self.groups.append(proc)
            try:
                return proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log(f"{label}: timed out after {timeout:.0f}s; killing its process group")
                kill_group(proc)
                return 124
            finally:
                kill_group(proc)

    def venv_python(self) -> str:
        return str(self.venv / "bin" / "python")

    def py_env(self, **extra: str) -> dict[str, str]:
        # PYTHONPATH wins over the venv's editable pointer, so the run imports this commit's code.
        return self.child_env(PYTHONPATH=str(self.tree / "server"), VIRTUAL_ENV=str(self.venv), **extra)

    # -- steps
    def prepare_tree(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        (self.scratch / "home").mkdir(parents=True, exist_ok=True)
        self.tree.parent.mkdir(parents=True, exist_ok=True)
        sh("git", "-C", str(self.repo), "worktree", "add", "--detach", "-q", str(self.tree), self.sha)

    def sync_venv(self) -> None:
        env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(self.venv)}
        done = subprocess.run(["uv", "sync", "--frozen", "--project", str(self.tree / "server")], env=env, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise StepFailed(f"uv sync failed: {done.stderr.strip()[-400:]}")

    def resolve_producers(self, assertions: list[str]) -> dict[str, dict]:
        """For each assertion id, the producer module, variant and provider that run it, read from this commit's producers."""

        code = (
            "import importlib, json, re, sys\n"
            "from pathlib import Path\n"
            "want = json.loads(sys.argv[1]); tree = Path(sys.argv[2])\n"
            "mods = ['zerg.qa.pi_helm_lifecycle', 'zerg.qa.omp_helm_lifecycle']\n"
            "out = {}\n"
            "for mod in mods:\n"
            "    src = (tree/'server'/(mod.replace('.', '/') + '.py')).read_text()\n"
            "    hits = [a for a in want if a in src]\n"
            "    if not hits: continue\n"
            "    m = importlib.import_module(mod)\n"
            "    p = getattr(m, 'PRODUCER', None); r = getattr(m, 'REGISTRATION', None)\n"
            "    if p is None or r is None or not p.variants: continue\n"
            "    for a in hits:\n"
            "        for v in p.variants:\n"
            "            if re.fullmatch(r'cell:[a-z0-9_]+:' + re.escape(a) + r':[a-z0-9_]+', v):\n"
            "                out[a] = {'module': r.executable_module, 'producer_id': r.producer_id, 'variant': v, 'provider': (r.providers or [''])[0], 'evidence_classes': list(r.evidence_classes)}\n"
            "print(json.dumps(out))\n"
        )
        done = subprocess.run(
            [self.venv_python(), "-c", code, json.dumps(assertions), str(self.tree)],
            cwd=self.tree, env=self.py_env(), capture_output=True, text=True, check=False,
        )  # fmt: skip
        if done.returncode != 0:
            raise StepFailed(f"cannot read the producers of this commit: {done.stderr.strip()[-400:]}")
        return json.loads(done.stdout.strip().splitlines()[-1])

    def provider_bins(self, providers: set[str]) -> dict[str, str]:
        """Install the CI-pinned provider CLIs (cached as one archive per set); returns the PATH entries to prepend."""

        root, home, cache = self.base / "providers" / "root", self.base / "providers" / "home", self.base / "providers" / "cache"
        if providers:
            with file_lock(self.base / "providers.lock"):
                done = subprocess.run(
                    [sys.executable, str(self.tree / "scripts/ci/terminal_truth_providers.py"), "install", "--providers", ",".join(sorted(providers)),
                     "--root", str(root), "--home", str(home), "--cache", str(cache)],
                    capture_output=True, text=True, check=False,
                )  # fmt: skip
                if done.returncode != 0:
                    raise StepFailed(f"provider install failed: {(done.stderr or done.stdout).strip()[-400:]}")
        path = f"{root / 'node_modules' / '.bin'}{os.pathsep}{home / '.local' / 'bin'}"
        self.env_base["PATH"] = f"{path}{os.pathsep}{self.env_base['PATH']}"
        if providers:
            verify = subprocess.run(
                [sys.executable, str(self.tree / "scripts/ci/terminal_truth_providers.py"), "verify", "--providers", ",".join(sorted(providers))],
                env={**os.environ, "PATH": self.env_base["PATH"]}, capture_output=True, text=True, check=False,
            )  # fmt: skip
            if verify.returncode != 0:
                raise StepFailed(f"provider versions differ from the pins: {(verify.stdout + verify.stderr).strip()[-400:]}")
        pins = {}
        import tomllib

        for name, entry in tomllib.loads((self.tree / "scripts/ci/terminal-truth-providers.toml").read_text()).items():
            pins[name] = entry
        return pins

    def engine(self) -> tuple[Path, Path]:
        """The engine and CLI for this commit: a cache hit by input digest, else one build in the persistent worktree."""

        rustc = subprocess.run(["rustc", "--version"], capture_output=True, text=True, check=False).stdout.strip()
        tree_listing = sh("git", "-C", str(self.tree), "ls-tree", "-r", self.sha, "--", "engine", "config", "schemas", "scripts/build", "server/pyproject.toml")
        digest = hashlib.sha256(f"{tree_listing}\n{rustc}\nci\n{'qa-fault-injection' if self.fault else ''}".encode()).hexdigest()[:24]
        cache = self.base / "bin-cache" / digest
        (self.base / "by-sha").mkdir(parents=True, exist_ok=True)
        (self.base / "by-sha" / self.sha).write_text(digest + "\n")
        if (cache / "longhouse-engine").is_file() and (cache / "longhouse").is_file():
            os.utime(cache)
            self.engine_note = f"cache hit {digest[:8]}"
            return cache / "longhouse-engine", cache / "longhouse"
        self.engine_note = f"built {digest[:8]}"
        with file_lock(self.base / "build.lock"):  # one heavy build at a time; a waiter re-checks the cache after the wait
            if (cache / "longhouse-engine").is_file() and (cache / "longhouse").is_file():
                self.engine_note = f"cache hit {digest[:8]} (built by another run)"
                return cache / "longhouse-engine", cache / "longhouse"
            build_tree = self.base / "trees" / "engine-build"
            if not (build_tree / ".git").exists():
                sh("git", "-C", str(self.repo), "worktree", "prune")
                sh("git", "-C", str(self.repo), "worktree", "add", "--detach", "-q", "-f", str(build_tree), self.sha)
            else:
                sh("git", "-C", str(build_tree), "checkout", "-q", "-f", "--detach", self.sha)
                sh("git", "-C", str(build_tree), "clean", "-fdq")
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(Path.home()),
                "LANG": "C.UTF-8",
                "LONGHOUSE_CARGO_TARGET_DIR": str(self.base / ("cargo-target-qa" if self.fault else "cargo-target")),
            }
            log_path = self.out / "engine-build.log"
            steps = [
                [sys.executable, "scripts/build/generate_build_identity.py", "--output", ".build/build-identity.json"],
                [sys.executable, "scripts/build/cargo.py", "exec", "--", "build", "--manifest-path", "engine/Cargo.toml", "--profile", "ci",
                 "--bin", "longhouse-engine", "--bin", "longhouse", *(["--features", "qa-fault-injection"] if self.fault else [])],
            ]  # fmt: skip
            for step in steps:
                if self.run(step, cwd=build_tree, env=env, log_path=log_path, timeout=900, label="engine build") != 0:
                    raise StepFailed(f"engine build failed; log {log_path}")
            staging = self.base / "bin-cache" / f".{digest}.{os.getpid()}"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            for name in ("longhouse-engine", "longhouse"):
                artifact = subprocess.run(
                    [sys.executable, "scripts/build/cargo.py", "artifact", "--profile", "ci", "--bin", name],
                    cwd=build_tree, env=env, capture_output=True, text=True, check=False,
                ).stdout.strip().splitlines()[-1]  # fmt: skip
                shutil.copy2(artifact, staging / name)
            shutil.rmtree(cache, ignore_errors=True)
            staging.rename(cache)
        entries = sorted((p for p in (self.base / "bin-cache").iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.stat().st_mtime)
        for old in entries[:-6]:
            shutil.rmtree(old, ignore_errors=True)
        return cache / "longhouse-engine", cache / "longhouse"

    # -- cells
    def run_tt_group(self, scenario: str, warm: bool, cells: list[dict], engine: Path, cli: Path, verdicts: dict[str, dict]) -> None:
        # The gate's own options (terminal_truth_parallel.standard_options): a Helm live-kinds cell opens its session first.
        label = f"{scenario}{'-warm' if warm else ''}"
        out_json = self.out / f"tt-{label}.json"
        argv = [self.venv_python(), "-m", "zerg.qa.terminal_truth", "run", "--scenario", scenario, "--cold-cell", "none", "--keep",
                "--engine", str(engine), "--longhouse-cli", str(cli), "--out", str(out_json)]  # fmt: skip
        if scenario in KIND_SCENARIOS:
            argv.append("--expect-kinds")
        if warm:
            argv.append("--warm-session")
        for cell in cells:
            argv += ["--cell", cell["key"]]
        t0 = time.monotonic()
        code = self.run(argv, cwd=self.tree, env=self.py_env(), log_path=self.out / f"tt-{label}.log", timeout=(self.cell_timeout or CELL_TIMEOUT_S) * max(1, len(cells)), label=f"terminal truth {label}")
        seconds = time.monotonic() - t0
        rows = {}
        if out_json.is_file():
            judged = subprocess.run(
                [self.venv_python(), "-c", JUDGE, str(out_json)], cwd=self.tree, env=self.py_env(), capture_output=True, text=True, check=False
            )  # fmt: skip
            if judged.returncode == 0:
                rows = {r["cell"]: r for r in json.loads(judged.stdout.strip().splitlines()[-1])}
        any_fail = False
        for cell in cells:
            row = rows.get(cell["key"])
            if row is None:
                reason = "timed out" if code == 124 else f"cell produced no result (terminal_truth exit {code})"
                verdicts[cell["name"]] = {"verdict": "FAIL", "reason": reason, "seconds": seconds, "evidence": f"tt-{label}.log"}
                any_fail = True
            else:
                verdicts[cell["name"]] = {"verdict": "PASS" if row["ok"] else "FAIL", "reason": row["detail"], "seconds": seconds, "evidence": f"tt-{label}.json"}
                any_fail |= not row["ok"]
        if any_fail:
            self.keep_scratch_evidence(f"tt-{label}")

    def keep_scratch_evidence(self, label: str) -> None:
        """A failing run's PTY recordings, host and engine logs live in scratch; copy the small files before teardown."""

        dest = self.out / f"scratch-copy-{label}"
        total = 0
        for path in sorted(self.scratch.rglob("*")):
            try:
                if not path.is_file() or path.is_symlink() or "node_modules" in path.parts or ".cache" in path.parts:
                    continue
                size = path.stat().st_size
                if size > 8 * 1024 * 1024 or total + size > 64 * 1024 * 1024 or path.suffix not in {".log", ".json", ".raw", ".txt", ".jsonl", ".md"}:
                    continue
                target = dest / path.relative_to(self.scratch)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                total += size
            except OSError:
                continue

    def run_producer(self, producer_id: str, module: str, info: dict, cells: list[dict], engine: Path, cli: Path, pins: dict, verdicts: dict[str, dict]) -> None:
        provider, model = PROOF_PRODUCERS[producer_id]
        evidence = self.out / "producers" / producer_id / "ev"
        driver_log = self.out / "producers" / producer_id / "run.log"
        argv = [
            self.venv_python(), str(self.tool_path), "--producer-driver",
            "--scratch", str(self.scratch / "producer"), "--evidence-root", str(evidence), "--module", module, "--variant", cells[0]["variant"],
            "--repo-root", str(self.tree), "--engine", str(engine), "--longhouse-cli", str(cli),
            "--provider-bin", shutil.which(provider, path=self.env_base["PATH"]) or provider,
            "--provider-version", pins[provider]["version"], "--model", model,
        ]  # fmt: skip
        timeout = self.cell_timeout or PRODUCER_TIMEOUT_S
        env = self.py_env()
        receipts = evidence.parent / "fault-receipts.jsonl"
        if self.fault:
            argv = fault_wrapper(argv, env, self.fault, receipts, timeout)
        t0 = time.monotonic()
        code = self.run(argv, cwd=self.tree, env=env if not self.fault else {"PATH": env["PATH"]}, log_path=driver_log, timeout=timeout + (60 if self.fault else 0), label=producer_id)
        seconds = time.monotonic() - t0
        result_path = evidence / "result.json"
        result = None
        if result_path.is_file():
            with contextlib.suppress(ValueError, OSError):
                result = json.loads(result_path.read_text())
        rel = f"producers/{producer_id}/ev/result.json" if result else f"producers/{producer_id}/run.log"
        any_fail = False
        if self.fault and result is not None:
            for cell in cells:
                verdicts[cell["name"]] = control_verdict(result, receipts, cell["assertion"], self.fault, seconds, rel)
                any_fail |= verdicts[cell["name"]]["verdict"] != "PASS"
            if any_fail:
                self.keep_scratch_evidence(producer_id)
            return
        for cell in cells:
            if result is None:
                reason = "timed out" if code == 124 else f"producer wrote no result (exit {code}); last log: {tail_text(driver_log)}"
                verdicts[cell["name"]] = {"verdict": "FAIL", "reason": reason, "seconds": seconds, "evidence": rel}
                any_fail = True
                continue
            value = (result.get("assertions") or {}).get(cell["assertion"])
            if value is True:
                verdicts[cell["name"]] = {"verdict": "PASS", "reason": "", "seconds": seconds, "evidence": rel}
            else:
                why = "assertion false" if value is False else "assertion not reached"
                error = result.get("error") if isinstance(result.get("error"), str) else ""
                extra = error[:200] or result.get("failure_code") or result.get("status")
                verdicts[cell["name"]] = {"verdict": "FAIL", "reason": f"{why}: {extra}", "seconds": seconds, "evidence": rel}
                any_fail = True
        if any_fail:
            self.keep_scratch_evidence(producer_id)

    # -- teardown
    def teardown(self) -> dict:
        leftovers: list[str] = []
        for proc in self.groups:
            kill_group(proc)
        for _ in range(3):
            stray = self.stray_processes()
            if not stray:
                break
            for pid, cmd in stray:
                leftovers.append(f"{pid} {cmd[:100]}")
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
        remaining = self.stray_processes()
        self.reap_semaphores()
        if not self.keep:
            shutil.rmtree(self.scratch, ignore_errors=True)
            shutil.rmtree(self.venv, ignore_errors=True)
            if self.tree.exists():
                subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(self.tree)], capture_output=True, check=False)
                shutil.rmtree(self.tree, ignore_errors=True)
            subprocess.run(["git", "-C", str(self.repo), "worktree", "prune"], capture_output=True, check=False)
        gone = self.keep or not (self.scratch.exists() or self.venv.exists() or self.tree.exists())
        state = "clean" if not remaining and gone else "DIRTY"
        detail = ""
        if remaining:
            detail += "processes alive: " + "; ".join(f"{p} {c[:80]}" for p, c in remaining) + ". "
        if not gone:
            detail += f"left on the proof host: {[str(p) for p in (self.scratch, self.venv, self.tree) if p.exists()]}"
        if leftovers:
            detail += f" (killed {len(leftovers)} stray process(es): {leftovers[:3]})"
        return {"state": state, "detail": detail.strip(), "killed_strays": len(leftovers)}

    def reap_semaphores(self) -> None:
        """A process that is killed leaves its multiprocessing semaphores in /dev/shm (30 per killed host, 2026-11-15).

        Remove the ones this user owns, that were created during this run and that no live process maps."""

        shm = Path("/dev/shm")
        mapped: set[str] = set()
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                with contextlib.suppress(OSError):
                    mapped.update(line.split()[-1] for line in (entry / "maps").read_text().splitlines() if "/dev/shm/sem." in line)
        started_wall = time.time() - (time.monotonic() - self.started) - 5
        for path in shm.glob("sem.mp-*"):
            with contextlib.suppress(OSError):
                info = path.stat()
                if info.st_uid == os.getuid() and info.st_mtime >= started_wall and str(path) not in mapped:
                    path.unlink()

    def stray_processes(self) -> list[tuple[int, str]]:
        """Processes that belong to this run: they carry its marker, or live in or run from its scratch, venv or worktree."""

        mine = os.getpid()
        needles = [str(self.scratch), str(self.venv), str(self.tree), f"prove-{self.run_id}"]
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == mine:
                continue
            try:
                cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
                environ = (entry / "environ").read_bytes()
                cwd = os.readlink(entry / "cwd")
            except OSError:
                continue
            if f"{MARKER_ENV}={self.run_id}".encode() in environ or any(n in cmd or cwd.startswith(n) for n in needles[:3]):
                found.append((int(entry.name), cmd))
        return found

    tool_path = Path(__file__).resolve()


class StepFailed(Exception):
    pass


def sh(*argv: str) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise StepFailed(f"{' '.join(argv[:4])} failed: {(done.stderr or done.stdout).strip()[-300:]}")
    return done.stdout


def tail_text(path: Path, n: int = 300) -> str:
    with contextlib.suppress(OSError):
        return path.read_text(errors="replace")[-n:].replace("\n", " | ")
    return ""


def kill_group(proc: subprocess.Popen) -> None:
    """Kill a child's process group (it was started with its own session), TERM first."""

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.wait(timeout=1)


@contextlib.contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# The Terminal Truth gate's own judgement of each row, applied to the rows one `terminal_truth run` wrote.
JUDGE = r"""
import json, sys
from zerg.qa.terminal_truth_kinds import SCENARIOS, kinds_expectation
rows = json.load(open(sys.argv[1]))
out = []
for r in rows:
    cell = r.get("cell")
    scenario = r.get("scenario", "long_reply")
    outcome = r.get("outcome")
    if scenario in SCENARIOS:
        e = kinds_expectation(r)
        if outcome == "fail":
            ok, detail = False, f"fail: {r.get('reason') or r.get('first_failed_stage') or 'see result'}"
        elif outcome == "not_measured":
            ok, detail = False, f"not measured: {r.get('reason') or 'cell could not run'}"
        elif e is not None and not e["ok"]:
            ok, detail = False, f"live item stopped at {e['stage']} (engine_posted={e['engine_posted']}, stream={e['stream']})"
        else:
            ok, detail = True, "" if e is not None else "measured (no kind expectation for this cell)"
    else:
        ok = outcome in {"pass", "known_bad", "not_measurable", "measured"}
        stage = r.get("first_failed_stage")
        detail = (f"stopped at {stage}: " if stage else "") + str(r.get("reason") or "") if not ok else ("declared known-bad (ledger)" if outcome == "known_bad" else "")
    out.append({"cell": cell, "ok": ok, "detail": detail})
print(json.dumps(out))
"""


def sweep_old_runs(base: Path, keep_id: str) -> None:
    runs = base / "runs"
    if not runs.is_dir():
        return
    now = time.time()
    for run in runs.iterdir():
        if run.name == keep_id or not run.is_dir():
            continue
        try:
            age = now - run.stat().st_mtime
        except OSError:
            continue
        if age > EVIDENCE_KEEP_S:
            shutil.rmtree(run, ignore_errors=True)
    # Worktrees and scratch of a run that died without teardown (the host rebooted, the process was SIGKILLed).
    trees = base / "trees"
    if trees.is_dir():
        for tree in trees.glob("run-*"):
            rid = tree.name[len("run-"):]
            if rid != keep_id and time.time() - tree.stat().st_mtime > 6 * 3600:
                subprocess.run(["git", "-C", str(base / "repo.git"), "worktree", "remove", "--force", str(tree)], capture_output=True, check=False)
                shutil.rmtree(tree, ignore_errors=True)
    for scratch in [*Path("/dev/shm").glob("prove-*"), *Path("/dev/shm").glob("lp" + "[0-9a-f]" * 6)]:
        with contextlib.suppress(OSError):
            if time.time() - scratch.stat().st_mtime > 6 * 3600:
                shutil.rmtree(scratch, ignore_errors=True)


def on_host(args: argparse.Namespace) -> int:
    run = ProofRun(args.run_id, args.sha, args.keep, args.deadline, args.cell_timeout, args.fault)
    run.run_dir.mkdir(parents=True, exist_ok=True)
    sweep_old_runs(run.base, args.run_id)

    def interrupted(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, interrupted)
    verdicts: dict[str, dict] = {}
    order = parse_cells(args.cells)
    cleanup = {"state": "unknown", "detail": "teardown did not run"}
    try:
        try:
            with run.phase("tree"):
                run.prepare_tree()
            with run.phase("venv"):
                run.sync_venv()
            tt, producer_cells = [], []
            for name in order:
                kind, info = classify(name)
                if kind == "tt":
                    tt.append({**info, "name": name})
                else:
                    producer_cells.append({"name": name, "assertion": info["assertion"]})
            resolved = {}
            if producer_cells:
                with run.phase("resolve"):
                    resolved = run.resolve_producers([c["assertion"] for c in producer_cells])
            runnable_producers: dict[str, list[dict]] = {}
            for cell in producer_cells:
                info = resolved.get(cell["assertion"])
                if info is None:
                    verdicts[cell["name"]] = {"verdict": "FAIL", "reason": "unknown cell: no producer of this commit has this assertion id", "seconds": 0, "evidence": ""}
                elif info["producer_id"] not in PROOF_PRODUCERS or "scripted_model" not in info["evidence_classes"]:
                    verdicts[cell["name"]] = {
                        "verdict": "FAIL", "seconds": 0, "evidence": "",
                        "reason": f"cannot run on the proof host: producer {info['producer_id']} has no verified scripted-model envelope here (supported: {', '.join(PROOF_PRODUCERS)})",
                    }  # fmt: skip
                else:
                    runnable_producers.setdefault(info["producer_id"], []).append({**cell, **info})
            if tt or runnable_producers:
                providers = {c["provider"] for c in tt if c["provider"] not in TT_FAKE_PROVIDERS} | {
                    PROOF_PRODUCERS[p][0] for p in runnable_producers
                }
                with run.phase("providers"):
                    pins = run.provider_bins(providers)
                with run.phase("engine"):
                    engine, cli = run.engine()
                with run.phase("cells"):
                    groups: dict[tuple[str, bool], list[dict]] = {}
                    for cell in tt:
                        warm = cell["scenario"] in KIND_SCENARIOS and cell["mode"] == "helm"
                        groups.setdefault((cell["scenario"], warm), []).append(cell)
                    for (scenario, warm), cells in groups.items():
                        if run.remaining() < 30:
                            for cell in cells:
                                verdicts[cell["name"]] = {"verdict": "FAIL", "reason": "run deadline reached before this cell started", "seconds": 0, "evidence": ""}
                            continue
                        run.run_tt_group(scenario, warm, cells, engine, cli, verdicts)
                    for producer_id, cells in runnable_producers.items():
                        if run.remaining() < 30:
                            for cell in cells:
                                verdicts[cell["name"]] = {"verdict": "FAIL", "reason": "run deadline reached before this cell started", "seconds": 0, "evidence": ""}
                            continue
                        run.run_producer(producer_id, cells[0]["module"], cells[0], cells, engine, cli, pins, verdicts)
        except StepFailed as exc:
            for name in order:
                verdicts.setdefault(name, {"verdict": "FAIL", "reason": f"setup failed: {exc}", "seconds": 0, "evidence": "run.log"})
        except KeyboardInterrupt as exc:
            for name in order:
                verdicts.setdefault(name, {"verdict": "FAIL", "reason": f"run interrupted ({exc})", "seconds": 0, "evidence": "run.log"})
    finally:
        cleanup = run.teardown()
        report = {
            "cells": [{"cell": name, **verdicts.get(name, {"verdict": "FAIL", "reason": "no verdict", "seconds": 0, "evidence": ""})} for name in order],
            "cleanup": cleanup,
            "phases": run.phases,
            "engine": run.engine_note,
        }
        (run.out / "verdicts.json").write_text(json.dumps(report, indent=2) + "\n")
        try:
            print("VERDICTS " + json.dumps(report), flush=True)
        except BrokenPipeError:
            pass
    return 0


# --------------------------------------------------------------------------------------------------------------------
# Producer driver: runs under the commit's venv python on the proof host
# --------------------------------------------------------------------------------------------------------------------


def producer_driver(args: argparse.Namespace) -> int:
    """Start a scratch Runtime Host, run one producer against it with the scripted model, stop the host."""

    from zerg.qa.terminal_truth_providers import ScratchRuntimeHost  # the commit's own host, as Terminal Truth starts it

    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    host = ScratchRuntimeHost(scratch / "host", device_id="prove")
    (scratch / "host").mkdir(parents=True, exist_ok=True)
    try:
        host.start()
        argv = [
            sys.executable, "-m", args.module, "--variant", args.variant, "--evidence-root", args.evidence_root, "--repo-root", args.repo_root,
            "--engine", args.engine, "--longhouse-cli", args.longhouse_cli, "--provider-bin", args.provider_bin,
            "--provider-version", args.provider_version, "--evidence-class", "scripted_model", "--api-url", host.url,
            "--agents-token", host.token, "--model", args.model,
        ]  # fmt: skip
        proc = subprocess.run(argv, check=False)
        return proc.returncode
    finally:
        host.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cells", default=os.environ.get("CELLS", ""))
    parser.add_argument("--sha", default=os.environ.get("SHA") or "HEAD")
    parser.add_argument("--target", default=os.environ.get("TARGET") or "proof", help="proof (default: the proof host); factory and wisp are not built")
    parser.add_argument("--deadline", type=int, default=DEFAULT_DEADLINE_S, help="seconds the whole run may take, teardown included")
    parser.add_argument("--cell-timeout", type=int, default=0, help="seconds one cell (or producer run) may take; default %d for a Terminal Truth cell, %d for a producer" % (CELL_TIMEOUT_S, PRODUCER_TIMEOUT_S))
    parser.add_argument("--fault", default=os.environ.get("FAULT") or "", help="run ONE producer cell as a negative control: the QA engine is told this fault through the engine-only channel (the producer is not), and PASS means the oracle caught it")
    parser.add_argument("--keep", action="store_true", help="leave the proof host's scratch, venv and worktree for debugging (swept after 6 h)")
    parser.add_argument("--on-host", action="store_true", help="internal: the proof host half")
    parser.add_argument("--run-id")
    parser.add_argument("--producer-driver", action="store_true", help="internal")
    for name in ("scratch", "evidence-root", "module", "variant", "repo-root", "engine", "longhouse-cli", "provider-bin", "provider-version", "model"):
        parser.add_argument(f"--{name}", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.producer_driver:
        return producer_driver(args)
    if args.on_host:
        return on_host(args)
    return orchestrate(args)


if __name__ == "__main__":
    raise SystemExit(main())
