"""Scenarios: what one cell does (a long reply, or two messages around a tool call), a pass of cells, a suite."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from zerg.qa.pty_reader import load_timed_chunks
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.scripted_model_server import REASONING_WORD
from zerg.qa.scripted_model_server import Pacing
from zerg.qa.scripted_model_server import pace_schedule
from zerg.qa.scripted_model_server import paced_text
from zerg.qa.terminal_truth_lanes import FileWatcher
from zerg.qa.terminal_truth_lanes import LaneWatcher
from zerg.qa.terminal_truth_lanes import RuntimeApi
from zerg.qa.terminal_truth_lanes import StreamWatcher
from zerg.qa.terminal_truth_lanes import TeeProxy
from zerg.qa.terminal_truth_lanes import host_publishes
from zerg.qa.terminal_truth_lanes import lane_stats
from zerg.qa.terminal_truth_lanes import pacing_error
from zerg.qa.terminal_truth_providers import CODEX_HELM_FLAGS
from zerg.qa.terminal_truth_providers import PROVIDERS
from zerg.qa.terminal_truth_providers import ProviderDriver
from zerg.qa.terminal_truth_providers import ScratchRuntimeHost
from zerg.qa.terminal_truth_providers import ScriptedModel
from zerg.qa.terminal_truth_providers import _prepare_provider
from zerg.qa.terminal_truth_providers import _version
from zerg.qa.terminal_truth_providers import pace_json
from zerg.qa.terminal_truth_providers import provider_environment
from zerg.qa.terminal_truth_pty import COLUMNS
from zerg.qa.terminal_truth_pty import REPO_ROOT
from zerg.qa.terminal_truth_pty import ROWS
from zerg.qa.terminal_truth_pty import _new_scratch
from zerg.qa.terminal_truth_pty import _tail_text
from zerg.qa.terminal_truth_pty import _wait_ready
from zerg.qa.terminal_truth_pty import terminal_first_seen
from zerg.qa.terminal_truth_verdicts import CellResult
from zerg.qa.terminal_truth_verdicts import CellSpec
from zerg.qa.terminal_truth_verdicts import Expectation
from zerg.qa.terminal_truth_verdicts import Observation
from zerg.qa.terminal_truth_verdicts import StageResult
from zerg.qa.terminal_truth_verdicts import check_live_previews
from zerg.qa.terminal_truth_verdicts import check_two_message_previews
from zerg.qa.terminal_truth_verdicts import first_failed_stage
from zerg.qa.terminal_truth_verdicts import judge
from zerg.qa.terminal_truth_verdicts import load_expectations
from zerg.qa.terminal_truth_verdicts import outcome_for


@dataclass
class CellPlan:
    """One cell of a pass: its nonce, the turns it adds to the shared model, its markers and prompt."""

    spec: CellSpec
    nonce: str
    pace: Pacing  # the long reply, or the first message of the two-message scenario
    pace_b: Pacing | None = None
    messages: list[str] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)
    tool_token: str | None = None

    @property
    def prompt(self) -> str:
        return f"TTRUTH{self.nonce} please stream the long reply"

    @property
    def turn_ids(self) -> list[str]:
        return [t["id"] for t in self.turns]


# Providers whose client misbehaves when the fake sends reasoning, so their cells run without it. Measured 2026-11-11:
# Claude Code 2.1.322 repeats the request (4 requests, the reply streamed 4 times) when the response carries a thinking
# block from the fake, signed or not. The cell result records that reasoning was off.
NO_REASONING_FAKE = {"claude"}


def plan_cell(spec: CellSpec, base: Pacing) -> CellPlan:
    if spec.provider in NO_REASONING_FAKE:
        base = dataclasses.replace(base, reasoning_tokens=0)
    nonce = secrets.token_hex(3)
    scope = "any_user" if spec.provider == "antigravity" else "latest"
    if spec.scenario == "two_message":
        nonce_b = secrets.token_hex(3)
        half = max(base.marker_every + 1, (base.tokens or 40) // 2)
        pace_a = dataclasses.replace(base, marker_prefix=f"TT{nonce}", tokens=half)
        pace_b = dataclasses.replace(base, marker_prefix=f"TT{nonce_b}", tokens=half, reasoning_tokens=0)
        tool_token = f"TTOOL{nonce}"
        text_a, text_b = paced_text(pace_a), paced_text(pace_b)
        turns = [
            {  # second request: the provider sends the tool result, whose output carries the tool token
                "id": f"{spec.key}:b",
                "match": tool_token,
                "match_scope": "any_input",
                "reply": text_b,
                "pace": pace_json(pace_b),
            },
            {
                "id": f"{spec.key}:a",
                "match": f"TTRUTH{nonce}",
                "match_scope": scope,
                "reply": text_a,
                "tool_call": {"type": "shell", "command": f"echo {tool_token}"},
                "pace": pace_json(pace_a),
            },
        ]
        return CellPlan(spec, nonce, pace_a, pace_b, [text_a, text_b], turns, tool_token)
    pace = dataclasses.replace(base, marker_prefix=f"TT{nonce}")
    text = paced_text(pace)
    turn = {"id": spec.key, "match": f"TTRUTH{nonce}", "match_scope": scope, "pace": pace_json(pace)}
    return CellPlan(spec, nonce, pace, None, [text], [turn])


def plan_schedule(plan: CellPlan) -> list[tuple[int, str, float]]:
    """Marker (index, word, seconds after the stream opens). Message two's due times are unknown (a tool round trip comes first)."""

    first = pace_schedule(plan.pace, plan.messages[0])
    if plan.pace_b is None:
        return first
    return first + pace_schedule(plan.pace_b, plan.messages[1])


@dataclass
class Pass:
    """Infrastructure the cells of one pass share: Runtime Host, tee proxy, scripted model, Machine Agent and HOME."""

    root: Path
    host: ScratchRuntimeHost
    proxy: TeeProxy
    model: ScriptedModel
    shipper: Any
    home: Path
    environment: dict[str, str]
    cli: Path | None
    engine: Path
    timing: dict[str, float] = field(default_factory=dict)
    prepared: dict[str, float] = field(default_factory=dict)  # provider -> seconds to prepare its home
    binaries: dict[str, str] = field(default_factory=dict)
    # provider -> the environment only that provider's launches (Helm PTY) get; the engine's `environment` is the union
    provider_environments: dict[str, dict[str, str]] = field(default_factory=dict)


def build_pass(
    specs: Sequence[CellSpec],
    plans: dict[str, CellPlan],
    *,
    engine: Path,
    cli: Path | None,
    host_environment: dict[str, str] | None = None,
    log: Callable[[str], None] = lambda _m: None,
    log_all_frames: bool = False,
) -> Pass:
    from zerg.qa import live_session_toolkit as toolkit

    started = time.monotonic()
    root = _new_scratch()
    home = root / "h"
    home.mkdir()
    turns: list[dict[str, Any]] = []
    for spec in specs:
        turns += plans[spec.key].turns
    model = ScriptedModel(root, turns, log_all_frames=log_all_frames)
    host = ScratchRuntimeHost(root, host_environment)
    proxy: TeeProxy | None = None
    shipper = None
    try:
        model.start()
        providers = sorted({s.provider for s in specs})
        environment: dict[str, str] = {}
        per_provider: dict[str, dict[str, str]] = {}
        binaries: dict[str, str] = {}
        prepared: dict[str, float] = {}
        for provider in providers:
            t0 = time.monotonic()
            binary = shutil.which(PROVIDERS[provider].binary)
            if binary is None:
                raise RuntimeError(f"{PROVIDERS[provider].binary} is not installed on this machine")
            binaries[provider] = binary
            penv = provider_environment(provider, model.endpoint, model.script, home)
            if provider == "antigravity":
                # An absolute path: an ambient wrapper that resolves "$HOME/.local/bin/agy" finds nothing under this HOME.
                penv["LONGHOUSE_ANTIGRAVITY_BIN"] = binary
            workspace_root = root / "w" / provider
            workspace_root.mkdir(parents=True)
            modes = {s.mode for s in specs if s.provider == provider}
            _prepare_provider(provider, modes, home=home, root=root, cli=cli, binary=binary, penv=penv, workspace_root=workspace_root)
            per_provider[provider] = penv
            prepared[provider] = time.monotonic() - t0
        defaults = {"XDG_CONFIG_HOME": home / ".config", "PI_CODING_AGENT_DIR": home / ".pi" / "agent", "CODEX_HOME": home / ".codex"}
        for provider in providers:
            # A variable that only restates the provider's default location under HOME is dropped: exported to every
            # provider in the shared environment it moves the config roots of the others (OMP reads Pi's variable).
            for name, default in defaults.items():
                if per_provider[provider].get(name) == str(default):
                    per_provider[provider].pop(name)
        # ONE environment, the union, only for the one Machine Agent. It has to be one: the engine reads HOME,
        # CLAUDE_CONFIG_DIR, XDG_CONFIG_HOME and CODEX_HOME once at startup to find every provider's session roots
        # (engine/src/discovery.rs:34-50, 113-118) and a Console turn's provider child inherits the engine's environment
        # (engine/src/codex_exec.rs:1150-1166). A Helm launch does not use it: each Helm cell gets only its own provider's
        # variables (run_cell_in), so one provider's variables never reach another provider's terminal.
        for provider in providers:
            for key, value in per_provider[provider].items():
                if key in environment and environment[key] != value:
                    log(f"environment conflict on {key} between providers; keeping the first value")
                    continue
                environment[key] = value
        t0 = time.monotonic()
        host.start()
        host_s = time.monotonic() - t0
        proxy = TeeProxy(host.url)
        proxy.start()
        t0 = time.monotonic()
        args = argparse.Namespace(api_url=proxy.url, agents_token=host.token, engine=engine, repo_root=REPO_ROOT)
        shipper = toolkit.start_transcript_shipper(
            "claude" if "claude" in providers else providers[0], args, home=home, environment=environment, evidence_root=root / "evidence"
        )
        engine_s = time.monotonic() - t0
        timing = {"boot_host_s": host_s, "boot_engine_s": engine_s, "setup_s": time.monotonic() - started}
        # start_transcript_shipper adds LONGHOUSE_HOME to `environment` (the Helm launcher finds the engine through it)
        for provider in providers:
            per_provider[provider]["LONGHOUSE_HOME"] = environment["LONGHOUSE_HOME"]
        return Pass(root, host, proxy, model, shipper, home, environment, cli, engine, timing, prepared, binaries, per_provider)
    except BaseException:
        if shipper is not None:
            with contextlib.suppress(Exception):
                shipper.stop()
        if proxy is not None:
            proxy.close()
        host.stop()
        model.stop()
        shutil.rmtree(root, ignore_errors=True)
        raise


def end_codex_sessions(pas: Pass, sessions: Sequence[str]) -> dict[str, Any]:
    """Close a Codex Helm cell's managed session the way the product does, not by killing the terminal.

    Killing the PTY's process group makes the `longhouse codex` wrapper detach: the bridge, its `codex app-server`
    child and the `claude-channel serve` grandchild stay up on purpose so the session can be reattached
    (`longhouse codex attach`), and a detached bridge has no expiry. A harness that only kills the terminal therefore
    leaves three processes per cell, with parent pid 1, on the machine that ran it. The product's own end-of-session
    command ends them; anything still holding this pass's directory afterwards is recorded and killed.
    """

    stopped: list[str] = []
    for session in sessions:
        with contextlib.suppress(Exception):
            done = subprocess.run(
                [str(pas.engine), "codex-bridge", "stop", "--session-id", session, "--force", "--reason", "terminal_truth_cell_end"],
                env=pas.provider_environments["codex"],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if done.returncode == 0:
                stopped.append(session)
    return {"bridges_stopped": stopped}


def leaked_processes(root: Path) -> list[str]:
    """Processes whose command line still names ``root`` (a pass's scratch directory); each one is killed and reported."""

    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False).stdout
    leaked: list[str] = []
    for line in out.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        if str(root) in command and pid_text.isdigit() and int(pid_text) != os.getpid():
            leaked.append(f"{pid_text} {command[:160]}")
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(int(pid_text), signal.SIGKILL)
    return leaked


def stop_pass(pas: Pass, keep: bool) -> float:
    t0 = time.monotonic()
    with contextlib.suppress(Exception):
        pas.shipper.stop()
    pas.proxy.close()
    pas.host.stop()
    pas.model.stop()
    leaked = leaked_processes(pas.root)
    if leaked:
        print(f"terminal truth: {len(leaked)} process(es) outlived the pass and were killed:\n  " + "\n  ".join(leaked), flush=True)
    if not keep:
        shutil.rmtree(pas.root, ignore_errors=True)
    return time.monotonic() - t0


def run_cell_in(
    pas: Pass,
    plan: CellPlan,
    *,
    expectation: Expectation,
    base_pace: Pacing,
    grace_s: float,
    ready_timeout: float,
    drivers: dict[str, ProviderDriver],
) -> CellResult:
    """Run one cell inside a pass: its own workspace, session, nonce and observers; shared host, engine and model."""

    spec = plan.spec
    provider, mode = spec.provider, spec.mode
    driver = drivers[provider]
    binary = pas.binaries[provider]
    cell_started = time.monotonic()
    result = CellResult(provider=provider, mode=mode, status="could_not_run", scenario=spec.scenario, nonce=plan.nonce)
    result.expectation = {"live_reply_text": expectation.live_reply_text, "source": expectation.source, "reason": expectation.reason}
    result.provider_version = _version(binary)
    result.engine_version = _version(str(pas.engine))
    result.annotations = {
        "provider_version": result.provider_version,
        "engine_version": result.engine_version,
        "reasoning_from_fake": "off (client repeats the request)" if provider in NO_REASONING_FAKE else base_pace.reasoning_tokens,
    }
    result.timing = {"prepare_home_s": pas.prepared.get(provider), "prepare_home_note": "per provider, shared by its cells"}
    schedule = plan_schedule(plan)
    markers = [word for _, word, _ in schedule]
    obs = Observation(
        provider=provider, mode=mode, markers=markers, messages=plan.messages, scenario=spec.scenario, tool_token=plan.tool_token
    )
    workspace = pas.root / "w" / provider / f"{mode}-{plan.nonce}"
    workspace.mkdir(parents=True)
    api = RuntimeApi(pas.host.url, pas.host.token, workspace=workspace)
    stop = threading.Event()
    session: ProviderPtySession | None = None
    recording, timing_file = pas.root / f"terminal-{provider}-{mode}.raw", pas.root / f"terminal-{provider}-{mode}.timing"
    watchers: list[LaneWatcher | StreamWatcher] = []
    try:
        watch_for = markers + ([plan.tool_token] if plan.tool_token else [])
        if mode != "console":
            cli = pas.cli
            if cli is None:
                raise RuntimeError("the native longhouse CLI is not installed on this machine")
            helm_argv = [
                str(cli), provider, "--cwd", str(workspace), "--url", pas.proxy.url,
                *(["--token", pas.host.token] if provider != "pi" else []),  # `longhouse pi` takes no --token (clap exit 2)
                f"--{provider}-bin", binary,
            ]  # fmt: skip
            if provider == "opencode":
                helm_argv += ["--model", "longhouse-mock/mock-model"]
            if provider == "pi":
                helm_argv += ["--provider", "longhouse-mock", "--model", "mock-model"]
            if provider == "codex":
                helm_argv += CODEX_HELM_FLAGS
            t0 = time.monotonic()
            session = ProviderPtySession.start(
                argv=driver.argv(Path(binary), workspace) if mode == "shadow" else helm_argv,
                cwd=workspace,
                env=pas.provider_environments[provider],
                terminal_path=recording,
                timing_path=timing_file,
                rows=ROWS,
                columns=COLUMNS,
            )
            obs.ready_evidence = _wait_ready(session, recording, timing_file, driver.ready_text, ready_timeout, driver.answers)
            result.timing["provider_start_to_ready_s"] = time.monotonic() - t0

        watchers = [LaneWatcher(lane, watch_for, stop) for lane in api.poll_lanes()]
        stream = StreamWatcher("workspace stream (SSE)", lambda: api.stream_lines(stop), watch_for, stop)
        stream.prompt = plan.prompt
        watchers.append(stream)
        console_session = ""
        if mode == "console":
            t0 = time.monotonic()
            created = _create_console_session(api, provider, pas.shipper.machine_name, workspace)
            console_session = str(created["session_id"])
            api.pinned_sessions = [console_session]  # the list endpoint may not show a headless session
            obs.ready_evidence = f"console session {console_session} created on machine {pas.shipper.machine_name}; adapter available"
            result.timing["provider_start_to_ready_s"] = time.monotonic() - t0
        # Codex also writes streamed text to debug SQLite logs; only its rollout file is the transcript.
        files = FileWatcher(pas.home, markers, stop, only_under=(".codex/sessions",) if provider == "codex" else ())
        files.scan_once()  # baseline of what already exists, so only writes after the prompt count
        for watcher in watchers:
            watcher.start()
        files.start()
        submitted = time.monotonic()
        if mode == "console":
            api.post(
                f"/api/agents/sessions/{console_session}/turns", {"message": plan.prompt, "client_request_id": f"tt-{secrets.token_hex(4)}"}
            )
        else:
            assert session is not None
            session.submit_line(plan.prompt)
        stream_seconds = base_pace.initial_delay + (len(plan.messages[0].split()) - 1) / base_pace.rate
        if plan.pace_b is not None:
            stream_seconds += 10 + base_pace.initial_delay + (len(plan.messages[1].split()) - 1) / base_pace.rate
        deadline = submitted + stream_seconds + grace_s + 30
        reference: dict[str, float | None] = {}
        polled_done_at: float | None = None

        def reference_times() -> dict[str, float | None]:
            if mode == "console":
                return pas.model.sent_times(markers)
            return terminal_first_seen(load_timed_chunks(recording, timing_file), markers)

        while time.monotonic() < deadline:
            reference = reference_times()
            seen_ref = [m for m in markers if reference.get(m) is not None]
            all_seen = len(seen_ref) == len(markers)
            polled = [w for w in watchers if isinstance(w, LaneWatcher)]
            if all_seen and polled_done_at is None and all(m in w.first_seen for w in polled for m in seen_ref):
                polled_done_at = time.monotonic()
            if polled_done_at is not None and time.monotonic() > polled_done_at + 8:
                break
            if all_seen and time.monotonic() > max(reference[m] for m in markers) + grace_s:  # type: ignore[type-var]
                break
            if session is not None and not session.alive():
                break
            time.sleep(1.0)
        settle_from = time.monotonic()
        stop.set()
        for watcher in watchers:
            watcher.join(timeout=5)
        files.join(timeout=5)
        reference = reference_times()
        last_ref = max((t for t in reference.values() if t is not None), default=None)
        result.timing["stream_s"] = (last_ref - submitted) if last_ref is not None else None
        result.timing["settle_s"] = time.monotonic() - settle_from
        result.status = "measured"
        result.baseline = "model stream (frame written to the provider)" if mode == "console" else "terminal screen (pyte)"
        result.pace = {
            "tokens": base_pace.tokens,
            "rate_per_s": base_pace.rate,
            "initial_delay_s": base_pace.initial_delay,
            "marker_every": base_pace.marker_every,
            "reasoning_tokens": plan.pace.reasoning_tokens,
        }
        result.stream_seconds = stream_seconds
        result.markers = markers
        sent = pas.model.sent_times(markers)
        result.terminal_first_seen_s = {m: (t - submitted if t is not None else None) for m, t in reference.items()}
        result.pacing_error_s = pacing_error(schedule, reference) if plan.pace_b is None else None
        if mode != "console":
            result.model_sent_s = {m: (t - submitted if t is not None else None) for m, t in sent.items()}
        text = plan.messages[-1]
        for watcher in watchers:
            stats = lane_stats(watcher.lane.name, reference, watcher.first_seen, watcher.resolution_s, markers, files.first_seen)
            stats.observations = len(watcher.response_times)
            result.lanes.append(stats)
            if watcher.errors:
                result.lane_errors[watcher.lane.name] = watcher.errors
            if isinstance(watcher, StreamWatcher):
                result.stream_sample = [
                    f"events: {watcher.event_counts}",
                    f"prompt echoes set aside: {watcher.prompt_echoes}",
                    *watcher.sample,
                ]
                result.final_preview = {
                    "expected_len": len(text),
                    "sessions": {
                        sid: {"len": len(v), "equals_final_message": v.strip() == text.strip()} for sid, v in watcher.final_preview.items()
                    },
                }
                result.live_preview_check = (
                    check_two_message_previews(watcher.previews, plan.messages, REASONING_WORD)
                    if plan.pace_b is not None
                    else check_live_previews(watcher.previews, text, REASONING_WORD)
                )
                obs.stream_first_seen = dict(watcher.first_seen)
                wall_at_submit = time.time() - (time.monotonic() - submitted)
                result.stream_stage_s = {
                    marker: {
                        "paint": (reference[marker] - submitted) if reference.get(marker) is not None else None,
                        **{name: (t - wall_at_submit if t is not None else None) for name, t in stage_times.items()},
                    }
                    for marker, stage_times in watcher.marker_stages.items()
                }
                obs.previews = watcher.previews
                obs.tool_on_client = bool(plan.tool_token and (plan.tool_token in watcher.first_seen or watcher.tool_steps))
        if plan.tool_token:
            workspace_lane = [w for w in watchers if isinstance(w, LaneWatcher) and "workspace" in w.lane.name]
            obs.tool_on_client = obs.tool_on_client or any(plan.tool_token in w.first_seen for w in workspace_lane)
        result.file_first_seen_s = {m: (files.first_seen[m] - submitted if m in files.first_seen else None) for m in markers}
        result.file_paths = sorted(set(files.first_path.values()))
        result.file_scan_gap_s = files.resolution_s
        publishes, longest, runtime_requests = host_publishes(pas.root, api.known_sessions)
        obs.reference, obs.model_sent = reference, sent
        obs.host_publishes, obs.host_publish_max_len = publishes, longest
        wanted = set(markers)
        obs.engine_posted = {m: t for m, t in pas.proxy.runtime_posts.items() if m in wanted}
        obs.engine_other_posts = sum(1 for m in markers if m in pas.proxy.other_posts and m not in pas.proxy.runtime_posts)
        result.diagnostics = {
            "cell_sessions": sorted(api.known_sessions),
            "host_publishes": publishes,
            "host_publish_max_text_len": longest,
            "host_runtime_events_requests_pass_total": runtime_requests,
            "tee_runtime_posts_pass_total": pas.proxy.runtime_post_count,
        }
        last = markers[-1]
        if reference.get(last) is not None:
            first_ref = next(reference[m] for m in markers if reference.get(m) is not None)
            result.split = {
                "stream_s": reference[last] - first_ref,
                "last_paint_s_after_submit": reference[last] - submitted,
                "provider_write_lag_after_last_paint_s": (files.first_seen[last] - reference[last]) if last in files.first_seen else None,
            }
    except Exception as exc:  # noqa: BLE001 - becomes a preflight failure carrying the cause
        obs.preflight_error = f"{type(exc).__name__}: {exc}"
        result.reason = obs.preflight_error
        if recording.exists() and "last screen" not in obs.preflight_error:
            result.reason += "\nlast output:\n" + _tail_text(recording)
    finally:
        stop.set()
        t0 = time.monotonic()
        if provider == "codex" and mode == "helm":
            result.diagnostics.update(end_codex_sessions(pas, sorted(api.known_sessions)))
        if session is not None:
            session.close()
        result.timing["teardown_s"] = time.monotonic() - t0
    obs.model_requests_served = pas.model.served_requests(plan.turn_ids)
    result.diagnostics["model_served_requests_for_cell"] = obs.model_requests_served
    stages = judge(obs)
    result.stages = {name: stage.to_json() for name, stage in stages.items()}
    result.first_failed_stage = first_failed_stage(stages)
    result.outcome = outcome_for(result.first_failed_stage, expectation, mode)
    if result.first_failed_stage is not None and not result.reason:
        result.reason = f"{result.first_failed_stage}: {stages[result.first_failed_stage].reason}"
    sse = next((lane for lane in result.lanes if "SSE" in lane.lane), None)
    worst = max(((d, m) for m, d in (sse.delays.items() if sse else []) if d is not None), default=None)
    result.worst_marker = {"marker": worst[1], "delay_s": worst[0]} if worst else {}
    result.timing["cell_wall_s"] = time.monotonic() - cell_started
    result.timing.update({f"pass_{k}": v for k, v in pas.timing.items()})
    return result


HOME_EXCLUSIVE = {"omp"}


def run_suite(
    specs: Sequence[CellSpec],
    *,
    pace: Pacing,
    engine: Path,
    longhouse_cli: Path | None = None,
    keep: bool = False,
    grace_s: float = 90.0,
    cold: str | None = "first",
    expectations: dict[tuple[str, str], Expectation] | None = None,
    ready_timeout: float = 120.0,
    drivers: dict[str, ProviderDriver] | None = None,
    host_environment: dict[str, str] | None = None,
    log: Callable[[str], None] = lambda _m: None,
) -> list[CellResult]:
    """Run ``specs``: the cold cell alone in a pass of its own (own host, engine and HOME), the rest in one shared pass.

    The cold pass keeps the one-cell-one-host shape on every run, so a defect that appears only when cells share a host
    (or only when they do not) shows as a difference between the two.
    """

    expectations = expectations if expectations is not None else load_expectations()
    drivers = drivers or PROVIDERS
    cli = longhouse_cli or (Path(shutil.which("longhouse")) if shutil.which("longhouse") else None)

    def expectation_of(spec: CellSpec) -> Expectation:
        default = Expectation("informational" if spec.mode == "shadow" else "expected")
        return Expectation("informational") if spec.mode == "shadow" else expectations.get((spec.provider, spec.mode), default)

    def blank(spec: CellSpec, expectation: Expectation) -> dict[str, Any]:
        return {"live_reply_text": expectation.live_reply_text, "source": expectation.source, "reason": expectation.reason}

    runnable: list[CellSpec] = []
    results: list[CellResult] = []
    for spec in specs:
        expectation = expectation_of(spec)
        if expectation.live_reply_text == "not_measurable":
            results.append(
                CellResult(
                    spec.provider, spec.mode, "could_not_run", expectation.reason, scenario=spec.scenario, outcome="not_measurable",
                    expectation=blank(spec, expectation),
                )
            )  # fmt: skip
        else:
            runnable.append(spec)
    cold_spec: CellSpec | None = None
    if cold and runnable:
        cold_spec = runnable[0] if cold == "first" else next((s for s in runnable if s.key == cold), None)
    groups: list[tuple[list[CellSpec], str]] = []
    if cold_spec is not None:
        groups.append(([cold_spec], "cold"))
    rest = [s for s in runnable if s is not cold_spec]
    # OMP and Pi share a config layout, so in one HOME each breaks the other's launch (measured: OMP never selects the
    # scripted model, Pi's previews interleave). OMP's shared cells get a pass of their own.
    for apart in (False, True):
        part = [s for s in rest if (s.provider in HOME_EXCLUSIVE) == apart]
        if part:
            groups.append((part, "shared"))
    for group, shape in groups:
        plans = {s.key: plan_cell(s, pace) for s in group}
        try:
            pas = build_pass(group, plans, engine=engine, cli=cli, host_environment=host_environment, log=log)
        except Exception as exc:  # noqa: BLE001
            for spec in group:
                reason = f"pass setup failed: {type(exc).__name__}: {exc}"
                results.append(
                    CellResult(
                        spec.provider, spec.mode, "could_not_run", reason, scenario=spec.scenario, pass_shape=shape, outcome="fail",
                        first_failed_stage="preflight", stages={"preflight": StageResult(False, reason).to_json()},
                        expectation=blank(spec, expectation_of(spec)),
                    )
                )  # fmt: skip
            continue
        try:
            for spec in group:
                log(f"running {spec.key} ({shape} pass)")
                cell = run_cell_in(
                    pas,
                    plans[spec.key],
                    expectation=expectation_of(spec),
                    base_pace=pace,
                    grace_s=grace_s,
                    ready_timeout=ready_timeout,
                    drivers=drivers,
                )
                cell.pass_shape = shape
                cell.scratch = str(pas.root) if keep else None
                results.append(cell)
        finally:
            torn = stop_pass(pas, keep)
        for cell in results:
            if cell.pass_shape == shape and "pass_boot_host_s" in cell.timing:
                cell.timing["pass_teardown_s"] = torn
    return results


def _create_console_session(api: RuntimeApi, provider: str, device_id: str, cwd: Path) -> dict[str, Any]:
    payload = {
        "provider": provider,
        "device_id": device_id,
        "cwd": str(cwd),
        "project": "terminal-truth",
        "display_name": "terminal-truth",
        "launch_surface": "test",
    }
    deadline = time.monotonic() + 90
    while True:
        status, body = api.post_raw("/api/agents/sessions", payload)
        if status < 300:
            return json.loads(body)
        if status != 409 or "adapter_unavailable" not in body or time.monotonic() > deadline:
            raise RuntimeError(f"Console session create failed: HTTP {status} {body[:300]}")
        time.sleep(0.5)
