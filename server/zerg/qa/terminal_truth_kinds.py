"""Live kinds: what a Longhouse client shows, while a turn runs, of thinking, a running tool and a subagent.

The long-reply scenario (``terminal_truth_scenarios``) times streamed reply text. This module adds three scenarios that time the
other things a provider's own TUI shows during a turn, against the same two baselines (an owned PTY for Helm, the moment the scripted
model wrote the frame for Console) and the same lanes (SSE ``transcript_preview``, the durable events API, the workspace read) plus
the provider's own files and the engine's runtime posts, so a signal that never reaches the client says where it stopped.

- ``thinking``: the scripted model streams a paced reasoning block (one word per chunk, each word unique to the cell), then a short reply.
- ``tool``: the model asks for one shell command that prints a marker every second for six seconds; the command also appends each marker to
  a file in the workspace, so the harness knows when each output line really existed (Console has no terminal; Helm TUIs may hide output).
- ``subagent``: the model makes the parent spawn one subagent whose own scripted reply is long and paced.

A cell is ``measured`` only if its checks prove the scenario happened (the model sent the reasoning, the tool result carrying the last
output line came back to the model, the subagent's request reached the model). A scenario that did not happen is ``fail`` with the
reason, never an empty comparison. A provider with no subagent tool is ``not_measured`` with the reason.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import secrets
import statistics
import threading
import time
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from zerg.qa.pty_reader import load_timed_chunks
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.scripted_model_server import Pacing
from zerg.qa.scripted_model_server import pace_schedule
from zerg.qa.scripted_model_server import paced_text
from zerg.qa.terminal_truth_lanes import TRANSCRIPT_ROOTS
from zerg.qa.terminal_truth_lanes import FileWatcher
from zerg.qa.terminal_truth_lanes import LaneWatcher
from zerg.qa.terminal_truth_lanes import RuntimeApi
from zerg.qa.terminal_truth_lanes import Spans
from zerg.qa.terminal_truth_lanes import StateObserver
from zerg.qa.terminal_truth_lanes import StreamWatcher
from zerg.qa.terminal_truth_lanes import WokenStateReader
from zerg.qa.terminal_truth_lanes import _strip_text
from zerg.qa.terminal_truth_lanes import host_publishes
from zerg.qa.terminal_truth_lanes import state_snapshot
from zerg.qa.terminal_truth_lanes import woken_view_lags
from zerg.qa.terminal_truth_providers import CODEX_HELM_FLAGS
from zerg.qa.terminal_truth_providers import PROVIDERS
from zerg.qa.terminal_truth_providers import ProviderDriver
from zerg.qa.terminal_truth_providers import _version
from zerg.qa.terminal_truth_providers import pace_json
from zerg.qa.terminal_truth_pty import COLUMNS
from zerg.qa.terminal_truth_pty import ROWS
from zerg.qa.terminal_truth_pty import _tail_text
from zerg.qa.terminal_truth_pty import _wait_ready
from zerg.qa.terminal_truth_pty import terminal_first_seen
from zerg.qa.terminal_truth_scenarios import HOME_EXCLUSIVE
from zerg.qa.terminal_truth_scenarios import Pass
from zerg.qa.terminal_truth_scenarios import _create_console_session
from zerg.qa.terminal_truth_scenarios import build_pass
from zerg.qa.terminal_truth_scenarios import stop_pass
from zerg.qa.terminal_truth_verdicts import CellSpec

SCENARIOS = ("thinking", "tool", "subagent")

THINK_SECONDS = 8.0  # the reasoning block's length: the reply starts when it ends
THINK_WORDS = 8
TOOL_OUTPUTS = 6  # the command prints one marker per second this many times
SUB_TOKENS, SUB_RATE, SUB_EVERY = 60, 5.0, 10  # the subagent's reply: 12 s, a marker every 2 s
SIDE_FILE = ".tt-tool-side"  # relative: the command runs in the cell's workspace

# What the harness presses once, partway through, to open what a TUI collapses (record, never assume: the result says what was pressed).
# Not every key does something in every TUI; "default view" is what was on screen before the press.
# (provider, scenario) -> keys. Pi and OMP show thinking by default (ctrl+t would hide it), so only their tool output is expanded.
EXPAND_KEYS: dict[tuple[str, str], bytes] = {
    ("claude", "thinking"): b"\x0f",  # ctrl+o: transcript view
    ("claude", "tool"): b"\x0f",
    ("claude", "subagent"): b"\x0f",
    ("codex", "thinking"): b"\x14",  # ctrl+t: transcript overlay
    ("codex", "tool"): b"\x14",
    ("codex", "subagent"): b"\x14",
    ("pi", "tool"): b"\x0f",  # ctrl+o: expand tool output
    ("omp", "tool"): b"\x0f",
}
# The key is pressed this long after the scripted model wrote the scenario's anchor signal, so the part before it is the default view
# whatever the provider's start-up time is.
PRESS_AFTER_ANCHOR_S = {"thinking": 3.0, "tool": 2.5, "subagent": 4.0}
ANCHOR_GROUP = {"thinking": "thinking", "tool": "tool_started", "subagent": "subagent_started"}

# provider -> (tool name, arguments builder (prompt)) for the subagent scenario. Absent: no subagent tool, or not found yet.
SUBAGENT_TOOLS: dict[str, tuple[str, Callable[[str, str], dict[str, Any]]]] = {
    # (prompt, description): the description is what a TUI shows by default, so the spawn marker goes in both
    "claude": ("Agent", lambda p, d: {"description": d, "prompt": p, "subagent_type": "general-purpose"}),
    "opencode": ("task", lambda p, d: {"description": d, "prompt": p, "subagent_type": "general"}),
    "codex": ("spawn_agent", lambda p, d: {"message": p}),  # offered inside the multi_agent_v1 namespace once multi_agent_v2 is on
    "omp": (
        "task",
        lambda p, d: {"context": "ttruth", "i": "ttruth", "tasks": [{"task": p, "solutionSpace": "any", "name": "TtSub", "agent": "task"}]},
    ),
}
NO_SUBAGENT_REASON = {
    "pi": "Pi has no subagent tool by design",
    "antigravity": "agy offers `invoke_subagent` but only for a configured subagent type (a call with TypeName general failed: subagent not found "
    "or not allowed to be invoked); defining one was not attempted",
}


@dataclass(frozen=True)
class Signal:
    name: str
    group: str  # thinking | tool_started | tool_output | subagent_started | subagent_text | reply
    marker: str


@dataclass
class KindPlan:
    spec: CellSpec
    nonce: str
    turns: list[dict[str, Any]]
    signals: list[Signal]
    prompt: str
    expected_turn_ids: list[str]  # every one must be served, or the scenario did not happen
    model_groups: tuple[str, ...]  # groups the scripted model itself writes (its send time is the Console baseline)
    max_seconds: float
    side_file_groups: tuple[str, ...] = ()
    not_measured: str = ""

    @property
    def turn_ids(self) -> list[str]:
        return [t["id"] for t in self.turns]

    @property
    def markers(self) -> list[str]:
        return [s.marker for s in self.signals]

    def group(self, name: str) -> list[Signal]:
        return [s for s in self.signals if s.group == name]


def _pace(pace: Pacing) -> dict[str, Any]:
    out = pace_json(pace)
    if pace.reasoning_prefix:
        out["reasoning_prefix"] = pace.reasoning_prefix
    return out


def _marker(nonce: str, index: int) -> str:
    return f"TT{nonce}{index:04d}"


MUTATIONS = ("no_thinking", "no_tool", "no_subagent")  # each breaks its scenario on purpose; the cell must come out ``fail``


def plan_kind_cell(spec: CellSpec, mutation: str | None = None, *, text_before_tool: bool = False) -> KindPlan:
    """Plan one cell. ``mutation`` makes the scripted model skip the thing the scenario is about (negative control).

    ``text_before_tool`` (tool scenario): the model streams a sentence of reply text and then calls the tool in the same
    response, the usual shape of a real turn. Without it the tool call is the whole response and no reply text is near it.
    """

    scenario = spec.scenario
    nonce = secrets.token_hex(3)
    if spec.provider not in PROVIDERS:
        return KindPlan(
            spec,
            nonce,
            [],
            [],
            "",
            [],
            (),
            0,
            not_measured="the provider cannot be pointed at a fake endpoint (its model loop is server-side)",
        )
    scope = "any_user" if spec.provider == "antigravity" else "latest"
    b_scope = "any_input" if spec.provider == "antigravity" else "latest"  # agy appends its own context after a tool result
    prompt = f"TTRUTH{nonce} please run the {scenario} scenario"
    reply = Pacing(rate=20.0, initial_delay=0.5, tokens=6, marker_every=5, marker_prefix=f"TT{nonce}")
    reply_text = paced_text(reply)
    reply_signals = [Signal(f"reply_{i}", "reply", w) for i, (_, w, _) in enumerate(pace_schedule(reply, reply_text))]
    if scenario == "thinking":
        think = secrets.token_hex(3)
        pace = dataclasses.replace(
            reply, initial_delay=THINK_SECONDS, reasoning_tokens=THINK_WORDS, reasoning_prefix=f"TT{think}", tokens=21, marker_every=20
        )
        text = paced_text(pace)
        reply_signals = [Signal(f"reply_{i}", "reply", w) for i, (_, w, _) in enumerate(pace_schedule(pace, text))]
        signals = [Signal(f"thinking_{i}", "thinking", f"TT{think}{i:04d}") for i in range(THINK_WORDS)] + reply_signals
        if mutation == "no_thinking":
            pace = dataclasses.replace(pace, reasoning_tokens=0)
        turns = [{"id": spec.key, "match": f"TTRUTH{nonce}", "match_scope": scope, "pace": _pace(pace)}]
        return KindPlan(spec, nonce, turns, signals, prompt, [spec.key], ("thinking", "reply"), THINK_SECONDS + 30)
    if scenario == "tool":
        tool = secrets.token_hex(3)
        outputs = " ".join(str(i) for i in range(1, TOOL_OUTPUTS + 1))
        # `:` with a marker argument puts a marker in the tool call itself and prints nothing; each output line is a distinct marker.
        command = f": TT{tool}0000; for i in {outputs}; do echo TT{tool}000$i | tee -a {SIDE_FILE} 2>/dev/null; sleep 1; done"
        last = _marker(tool, TOOL_OUTPUTS)
        turns = [
            {"id": f"{spec.key}:b", "match": last, "match_scope": b_scope, "pace": _pace(reply)},
            {"id": f"{spec.key}:a", "match": f"TTRUTH{nonce}", "match_scope": scope, "tool_call": {"type": "shell", "command": command}},
        ]
        if text_before_tool:
            lead = Pacing(rate=20.0, initial_delay=0.3, tokens=13, marker_every=4, marker_prefix=f"TT{secrets.token_hex(3)}")
            turns[1]["pace"] = _pace(lead)
        if mutation == "no_tool":  # the model answers at once and never asks for the command
            turns[1] = {"id": f"{spec.key}:a", "match": f"TTRUTH{nonce}", "match_scope": scope, "pace": _pace(reply)}
        signals = (
            [Signal("tool_started", "tool_started", _marker(tool, 0))]
            + [Signal(f"tool_output_{i}", "tool_output", _marker(tool, i)) for i in range(1, TOOL_OUTPUTS + 1)]
            + reply_signals
        )
        return KindPlan(
            spec, nonce, turns, signals, prompt, [f"{spec.key}:a", f"{spec.key}:b"], ("reply",), TOOL_OUTPUTS + 40, ("tool_output",)
        )
    if scenario == "subagent":
        entry = SUBAGENT_TOOLS.get(spec.provider)
        if entry is None:
            reason = NO_SUBAGENT_REASON.get(spec.provider, "no subagent tool wired for this provider yet")
            return KindPlan(spec, nonce, [], [], prompt, [], (), 0, not_measured=reason)
        name, build = entry
        spawn, sub = secrets.token_hex(3), secrets.token_hex(3)
        sub_pace = Pacing(rate=SUB_RATE, initial_delay=1.0, tokens=SUB_TOKENS, marker_every=SUB_EVERY, marker_prefix=f"TT{sub}")
        sub_text = paced_text(sub_pace)
        sub_signals = [Signal(f"subagent_text_{i}", "subagent_text", w) for i, (_, w, _) in enumerate(pace_schedule(sub_pace, sub_text))]
        sub_prompt = f"TTSUBTASK{nonce} spawn marker {_marker(spawn, 0)} then write your findings"
        turns = [
            {"id": f"{spec.key}:b", "match": sub_signals[-1].marker, "match_scope": b_scope, "pace": _pace(reply)},
            {"id": f"{spec.key}:sub", "match": f"TTSUBTASK{nonce}", "match_scope": "latest", "pace": _pace(sub_pace)},
            {
                "id": f"{spec.key}:a",
                "match": f"TTRUTH{nonce}",
                "match_scope": scope,
                "tool_call": {"name": name, "arguments": build(sub_prompt, f"ttruth subagent {_marker(spawn, 0)}")},
            },
        ]
        if mutation == "no_subagent":  # the model answers at once and never spawns the subagent
            turns[2] = {"id": f"{spec.key}:a", "match": f"TTRUTH{nonce}", "match_scope": scope, "pace": _pace(reply)}
        signals = [Signal("subagent_started", "subagent_started", _marker(spawn, 0))] + sub_signals + reply_signals
        ids = [f"{spec.key}:a", f"{spec.key}:sub", f"{spec.key}:b"]
        groups = ("subagent_text", "reply")
        if spec.provider == "codex":
            # Codex's spawn_agent returns at once (`{"agent_id": ...}`) and the parent answers then; the child's result reaches the
            # parent only as a notification after its turn, which asks the model for nothing. So the parent's turn is keyed on the
            # spawn result, and delayed, so the stream still shows the spawn when the answer begins. Every Codex spawn result
            # has that key, so the turn also needs this cell's prompt in the request: two Codex cells share one scripted model
            # in a run, and without it the second cell's parent got the first cell's answer (2026-11-13, twice in two runs).
            turns = [
                {
                    **t,
                    "match": "agent_id",
                    "match_also": f"TTRUTH{nonce}",
                    "match_scope": "any_input",
                    "pace": _pace(dataclasses.replace(reply, initial_delay=2.0)),
                }
                if t["id"] == f"{spec.key}:b"
                else t
                for t in turns
            ]
            if spec.mode == "console":
                # Codex interrupts a Console child when the parent's turn ends (its rollout ends `turn_aborted`, reason `interrupted`,
                # before any model request), so the child writes nothing: only the spawn is measurable.
                turns = [t for t in turns if t["id"] != f"{spec.key}:sub"]
                signals = [s for s in signals if s.group != "subagent_text"]
                ids, groups = [f"{spec.key}:a", f"{spec.key}:b"], ("reply",)
        return KindPlan(spec, nonce, turns, signals, prompt, ids, groups, SUB_TOKENS / SUB_RATE + 60)
    raise ValueError(f"unknown scenario {scenario!r}")


@dataclass
class KindObservation:
    """Everything one cell saw, before judgement. All times are monotonic seconds."""

    plan: KindPlan
    preflight_error: str | None = None
    ready_evidence: str = ""
    turns_served: dict[str, int] = field(default_factory=dict)
    model_sent: dict[str, float | None] = field(default_factory=dict)
    side: dict[str, float] = field(default_factory=dict)
    terminal_any: dict[str, float | None] = field(default_factory=dict)  # Helm only
    terminal_default: dict[str, float | None] = field(default_factory=dict)  # before the expand key
    press_t: float | None = None
    lanes: dict[str, dict[str, float]] = field(default_factory=dict)  # lane name -> marker -> first seen
    engine_runtime: dict[str, float] = field(default_factory=dict)
    engine_other: dict[str, float] = field(default_factory=dict)
    provider_file: dict[str, float] = field(default_factory=dict)
    state_timeline: list[tuple[float, dict[str, Any]]] = field(default_factory=list)  # the facts lane: (time, snapshot) per change
    state_reads: int = 0
    state_shaped: int = 0
    submitted: float = 0.0


def judge_kinds(obs: KindObservation) -> list[tuple[str, bool, str]]:
    """The checks that prove the scenario happened. Any false check makes the cell ``fail``: nothing is compared on a scenario that did not run."""

    plan = obs.plan
    checks: list[tuple[str, bool, str]] = []
    checks.append(("provider_ran", obs.preflight_error is None, obs.preflight_error or "provider started and took the prompt"))
    checks.append(("ready_evidence", bool(obs.ready_evidence.strip()), obs.ready_evidence[:120] or "no readiness evidence"))
    unserved = [t for t in plan.expected_turn_ids if obs.turns_served.get(t, 0) < 1]
    checks.append(
        ("every_scripted_turn_served", not unserved, f"unserved turns: {unserved}" if unserved else "every scripted turn reached the model")
    )
    model_markers = [s.marker for s in plan.signals if s.group in plan.model_groups]
    unsent = [m for m in model_markers if obs.model_sent.get(m) is None]
    checks.append(
        (
            "model_emitted_every_marker",
            not unsent,
            f"the scripted model never wrote {len(unsent)} of {len(model_markers)} markers it was scripted to write: {unsent[:3]}"
            if unsent
            else f"the model wrote {len(model_markers)} of {len(model_markers)}",
        )
    )
    if plan.side_file_groups:
        want = [s.marker for s in plan.signals if s.group in plan.side_file_groups]
        missing = [m for m in want if m not in obs.side]
        # The side file can be unwritable (a read-only sandbox). The model getting the last output line back is the proof then.
        ran = not unserved and plan.expected_turn_ids and obs.turns_served.get(plan.expected_turn_ids[-1], 0) >= 1
        checks.append(
            (
                "command_really_ran",
                not missing or bool(ran),
                f"side file lacks {len(missing)} of {len(want)} output lines"
                + ("; the tool result still reached the model" if ran else ""),
            )
        )
    return checks


# ---- the facts lane: is a thinking / tool / subagent state shown, and which --------------------------------------------------

STATE_LIVE_BY = 0.5  # a state is "live" when it was up before this share of the real window had passed, "late" after


def state_intervals(
    timeline: Sequence[tuple[float, dict[str, Any]]], predicate: Callable[[dict[str, Any]], bool], end_t: float
) -> list[tuple[float, float]]:
    """Spans during which one session's served facts satisfied ``predicate``, unioned over the cell's sessions.

    A snapshot holds until the next change of the same session; the last one holds until ``end_t``.
    """

    by_session: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    for t, snap in timeline:
        by_session.setdefault(str(snap.get("session_id")), []).append((t, snap))
    spans: list[tuple[float, float]] = []
    for rows in by_session.values():
        for i, (t, snap) in enumerate(rows):
            if predicate(snap):
                spans.append((t, rows[i + 1][0] if i + 1 < len(rows) else end_t))
    spans.sort()
    merged: list[tuple[float, float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _overlap(spans: Sequence[tuple[float, float]], w0: float, w1: float) -> list[tuple[float, float]]:
    return [(max(a, w0), min(b, w1)) for a, b in spans if min(b, w1) > max(a, w0)]


def classify_state(
    timeline: Sequence[tuple[float, dict[str, Any]]],
    predicate: Callable[[dict[str, Any]], bool],
    window: tuple[float, float] | None,
    end_t: float,
    *,
    describe: Callable[[dict[str, Any]], str | None] = lambda s: s.get("headline"),
) -> dict[str, Any]:
    """live / late / absent for one state during the span the scripted scenario really held it.

    ``window`` is when the thing was truly in progress (model time for thinking, the command's own output times for a tool). The state is
    ``live`` if the facts showed it before half the window had passed, ``late`` if only afterwards, ``absent`` if never inside the window.
    ``coverage`` is the share of the window it was shown. ``shown`` lists the distinct strings the strip would have drawn meanwhile.
    """

    if window is None or window[1] <= window[0]:
        return {"class": "no_window", "first_s": None, "lag_s": None, "coverage": None, "shown": []}
    w0, w1 = window
    spans = _overlap(state_intervals(timeline, predicate, end_t), w0, w1)
    shown: list[str] = []
    for t, snap in timeline:
        if predicate(snap) and w0 <= t <= w1:
            text = describe(snap)
            if text and text not in shown:
                shown.append(text)
    # a state already up when the window opened carries over: its text is whatever was drawn then
    for t, snap in timeline:
        if t < w0 and predicate(snap):
            text = describe(snap)
            if text and text not in shown:
                shown.append(text)
    if not spans:
        return {"class": "absent", "first_s": None, "lag_s": None, "coverage": 0.0, "shown": shown, "window_s": [w0, w1]}
    first = spans[0][0]
    covered = sum(b - a for a, b in spans)
    live = first <= w0 + STATE_LIVE_BY * (w1 - w0)
    return {
        "class": "live" if live else "late",
        "first_s": first,
        "lag_s": first - w0,
        "coverage": round(covered / (w1 - w0), 2),
        "shown": shown,
        "window_s": [w0, w1],
    }


def _tool_state(snap: dict[str, Any]) -> bool:
    return snap.get("activity_state") == "executing"


def _thinking_state(snap: dict[str, Any]) -> bool:
    return snap.get("activity_state") == "thinking"


def _subagent_predicate(provider: str) -> Callable[[dict[str, Any]], bool]:
    """A subagent is shown when the delegation facts list one, or the parent's own tool is the provider's subagent-spawning tool."""

    spawn = (SUBAGENT_TOOLS.get(provider) or ("",))[0].lower()

    def predicate(snap: dict[str, Any]) -> bool:
        if any(t.get("kind") == "subagent" for t in snap.get("tasks") or []):
            return True
        return bool(spawn) and str(snap.get("activity_tool") or "").lower() == spawn

    return predicate


def _subagent_describe(snap: dict[str, Any]) -> str | None:
    return snap.get("detail") or (f"{snap.get('headline')}" if snap.get("headline") else None)


def state_windows(obs: KindObservation) -> dict[str, tuple[float, float] | None]:
    """When each state was truly in progress, from the scripted model's own writes and the command's own output file, never from a lane."""

    plan = obs.plan

    def span(group: str, source: dict[str, float | None]) -> tuple[float, float] | None:
        times = [t for s in plan.group(group) if (t := source.get(s.marker)) is not None]
        return (min(times), max(times)) if times else None

    thinking = span("thinking", obs.model_sent)
    tool = span("tool_output", obs.side) or span("tool_output", obs.model_sent) or span("tool_output", obs.terminal_any)
    sub = span("subagent_text", obs.model_sent)
    started = span("subagent_started", obs.model_sent)
    if sub and started:
        sub = (started[0], sub[1])
    return {"thinking": thinking, "tool": tool, "subagent": sub}


def state_lane_result(obs: KindObservation, end_t: float) -> dict[str, Any]:
    """The facts-lane verdict for the cell's scenario, plus the evidence that the observer could see the facts at all."""

    scenario, provider = obs.plan.spec.scenario, obs.plan.spec.provider
    windows = state_windows(obs)
    baseline = [snap for t, snap in obs.state_timeline if t < obs.submitted]
    dirty = [s for s in baseline if s.get("activity_state") in {"thinking", "executing"} or (s.get("delegation_count") or 0) > 0]
    out: dict[str, Any] = {
        "reads": obs.state_reads,
        "shaped_reads": obs.state_shaped,
        "baseline_snapshots": len(baseline),
        "baseline_idle": not dirty,
        "changes": len(obs.state_timeline),
        "timeline": [
            {
                "t": round(t - obs.submitted, 2),
                **{k: snap.get(k) for k in ("headline", "activity_state", "activity_tool", "delegation_count", "detail")},
            }
            for t, snap in obs.state_timeline
        ][:80],
    }
    if obs.state_shaped == 0:
        out["verdict"] = {"class": "observer_blind", "reason": f"{obs.state_reads} workspace reads, none carried session.session_state"}
        return out
    if dirty:
        out["verdict"] = {
            "class": "observer_dirty",
            "reason": "the facts already showed thinking, a tool or a subagent before the prompt was sent",
        }
        return out
    if scenario == "thinking":
        verdict = classify_state(obs.state_timeline, _thinking_state, windows["thinking"], end_t)
    elif scenario == "tool":
        verdict = classify_state(
            obs.state_timeline,
            _tool_state,
            windows["tool"],
            end_t,
            describe=lambda s: f"{s.get('headline')}" + (f" [{s['activity_tool']}]" if s.get("activity_tool") else ""),
        )
    else:
        verdict = classify_state(obs.state_timeline, _subagent_predicate(provider), windows["subagent"], end_t, describe=_subagent_describe)
    out["verdict"] = {k: (_rel(v, obs.submitted) if k == "first_s" else v) for k, v in verdict.items()}
    if "window_s" in verdict:
        out["verdict"]["window_s"] = [_rel(x, obs.submitted) for x in verdict["window_s"]]
    return out


# ---- the run -----------------------------------------------------------------------------------------------------------


class SidePoller(threading.Thread):
    """When each output line of the tool command was really written (the command appends it to a file in the workspace)."""

    def __init__(self, path: Path, markers: Sequence[str], stop: threading.Event) -> None:
        super().__init__(daemon=True, name="side-poller")
        self.path, self.markers, self.first_seen, self._stop_event = path, list(markers), {}, stop

    def run(self) -> None:
        while not self._stop_event.is_set():
            with contextlib.suppress(OSError):
                data = self.path.read_text(errors="replace")
                now = time.monotonic()
                for marker in self.markers:
                    if marker not in self.first_seen and marker in data:
                        self.first_seen[marker] = now
            self._stop_event.wait(0.02)


def _find_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for item in value.values():
            found = _find_key(item, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


class SubagentProbe(threading.Thread):
    """What the parent's Longhouse view says about a running subagent, and when the subagent's own text appears in its child session.

    Polls, for every session of the cell: the workspace read (its ``delegation`` facts: how many background workers the host believes are
    running), the ``/subagents`` route (children linked to the parent by tool call), and each child's durable events (the subagent's text).
    """

    def __init__(self, api: RuntimeApi, markers: Sequence[str], stop: threading.Event) -> None:
        super().__init__(daemon=True, name="subagent-probe")
        self.api, self.markers, self._stop_event = api, list(markers), stop
        self.delegation_first: float | None = None
        self.delegation_first_value: Any = None
        self.delegation_last_value: Any = None
        self.children_first: float | None = None
        self.children: dict[str, dict[str, Any]] = {}
        self.child_marker_first: dict[str, float] = {}
        self.child_marker_session: dict[str, str] = {}
        self.errors: list[str] = []

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._once()
            except Exception as exc:  # noqa: BLE001 - a probe failure is data
                if len(self.errors) < 10:
                    self.errors.append(f"{type(exc).__name__}: {exc}")
            self._stop_event.wait(0.5)  # recorded, not judged; each pass is three or more reads of the host

    def _once(self) -> None:
        for sid in self.api.session_ids():
            now = time.monotonic()
            status, body = self.api.get(f"/api/timeline/sessions/{sid}/workspace", browser=True)
            if status == 200:
                with contextlib.suppress(ValueError):
                    delegation = _find_key(json.loads(body), "delegation")
                    if isinstance(delegation, dict):
                        self.delegation_last_value = delegation
                        if (delegation.get("count") or 0) > 0 and self.delegation_first is None:
                            self.delegation_first, self.delegation_first_value = now, delegation
            status, body = self.api.get(f"/api/agents/sessions/{sid}/subagents")
            if status != 200:
                continue
            for child in json.loads(body).get("children") or []:
                cid = str(child.get("session_id"))
                if cid not in self.children:
                    self.children[cid] = {
                        "parent": sid,
                        "first_listed": now,
                        **{k: child.get(k) for k in ("provider", "parent_tool_call_id", "title", "tool_calls", "assistant_messages")},
                    }
                    if self.children_first is None:
                        self.children_first = now
                self.children[cid]["last_visible_text_preview"] = child.get("last_visible_text_preview")
        for cid in list(self.children):
            status, body = self.api.get(f"/api/agents/sessions/{cid}/events?limit=1000")
            if status == 200:
                now = time.monotonic()
                for marker in self.markers:
                    if marker not in self.child_marker_first and marker in body:
                        self.child_marker_first[marker] = now
                        self.child_marker_session[marker] = cid


class FramingStreamWatcher(StreamWatcher):
    """The workspace SSE, plus every frame's time and the shape of its preview (kind, tool name and state), for the report."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.frames: list[dict[str, Any]] = []
        self.t0 = time.monotonic()

    def _record_preview(self, data: str) -> None:
        now = time.monotonic()
        try:
            frame = json.loads(data)
        except ValueError:
            frame = None
        if isinstance(frame, dict) and isinstance(frame.get("transcript_preview"), dict) and len(self.frames) < 3000:
            preview = frame["transcript_preview"]
            row = {k: preview[k] for k in preview if k not in {"text", "tool_input_json", "tool_output_text"}}
            row["t"] = now
            row["session_id"] = frame.get("session_id")
            for key in ("text", "tool_input_json", "tool_output_text"):
                value = preview.get(key)
                if isinstance(value, str):
                    row[f"{key}_len"] = len(value)
                    row[f"{key}_tail"] = value[-60:]
            self.frames.append(row)
        super()._record_preview(data)


def _helm_argv(cli: Path, provider: str, workspace: Path, binary: str, url: str, token: str) -> list[str]:
    argv = [
        str(cli), provider, "--cwd", str(workspace), "--url", url,
        *(["--token", token] if provider != "pi" else []),
        f"--{provider}-bin", binary,
    ]  # fmt: skip
    if provider == "opencode":
        argv += ["--model", "longhouse-mock/mock-model"]
    if provider == "pi":
        argv += ["--provider", "longhouse-mock", "--model", "mock-model"]
    if provider == "codex":
        argv += CODEX_HELM_FLAGS
    return argv


def _warm_helm_session(session: ProviderPtySession, api: RuntimeApi, nonce: str) -> None:
    """Send one throwaway turn so the Helm session exists on the host, with its stream open, before the scenario prompt.

    A Helm session is unknown to the host until its first event, and a stream client can only connect to a session it can list, so a
    frame posted in the first half second of the first prompt (a tool or subagent start) is posted before any client could be
    listening. A real client has the session open long before a later prompt. The warm-up text carries no marker, so no lane counts it.
    """

    session.submit_line(f"TTWARM{nonce} reply with the word ok")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not api.session_ids(fresh=True):
        time.sleep(0.2)
    # The warm-up reply finishes and the provider is idle again: the state the host serves went busy and came back to
    # rest, or WARM_UP_S passed (the fixed pause this was until 2026-11-13, and still the longest it waits).
    listed = time.monotonic()
    was_busy, at_rest = False, 0
    while time.monotonic() < listed + WARM_UP_S and at_rest < 2:
        time.sleep(0.3)
        busy = _any_session_busy(api)
        was_busy = was_busy or bool(busy)
        if busy is False and (was_busy or time.monotonic() > listed + WARM_UP_UNSEEN_S):
            at_rest += 1
        else:
            at_rest = 0


def _any_session_busy(api: RuntimeApi) -> bool | None:
    """Whether a session of the cell is thinking, running a tool or delegating, as the host serves it; None when it cannot be read."""

    try:
        snaps = []
        for sid in api.session_ids():
            status, body = api.get(f"/api/timeline/sessions/{sid}/workspace", browser=True)
            snaps.append(state_snapshot(body, sid) if status == 200 else None)
    except Exception:  # noqa: BLE001 - not readable now; the caller looks again
        return None
    if not snaps or any(snap is None for snap in snaps):
        return None
    return any(_busy(snap) for snap in snaps)


def _busy(snap: dict[str, Any]) -> bool:
    return snap.get("activity_state") in {"thinking", "executing"} or (snap.get("delegation_count") or 0) > 0


def turn_at_rest(
    polled: Sequence[tuple[float, dict[str, Any]]],
    woken: Sequence[tuple[float, dict[str, Any]]],
    sessions: Iterable[str],
    now: float,
) -> bool:
    """Whether the turn is over as a client is shown it, so that a cell has nothing left to see.

    True when, for every session of ``sessions`` the host still serves: the last state it served is at rest (not thinking,
    no tool, no delegated work), a client that reads only when woken holds that state, and it has been served for
    ``AT_REST_HOLD_S``. That is longer than ``WOKEN_STATE_BUDGET_S``, so a last state the host served and never announced
    is not passed over: the woken client does not hold it, this stays False, and the cell waits its whole ``SETTLE_S``
    and fails on the unshown state as before.
    """

    judged = 0
    for sid in sessions:
        served = [(t, snap) for t, snap in polled if snap.get("session_id") == sid]
        if not served:
            continue
        judged += 1
        since, last = served[-1]
        shown = [snap for _, snap in woken if snap.get("session_id") == sid]
        if _busy(last) or not shown or _strip_text(shown[-1]) != _strip_text(last) or now < since + AT_REST_HOLD_S:
            return False
    return judged > 0


STREAM_OPEN_WAIT_S = 15.0
# The longest a state the host serves may go unshown by a client that refetches only when the stream wakes it. The other live
# budgets are 1.0 s; a state that changes with no frame is unshown until an unrelated frame arrives, which is seconds.
WOKEN_STATE_BUDGET_S = 1.0
WOKEN_STATE_CHECK = "a client that reads state only when woken shows every served state"
# After the turn's last durable event: a cell leaves once the turn is at rest for a client (``turn_at_rest``) and no lane
# has seen anything new for AT_REST_HOLD_S; otherwise it looks for SETTLE_S, as every cell did until 2026-11-13.
AT_REST_HOLD_S = WOKEN_STATE_BUDGET_S + 0.5
SETTLE_S = 5.0
WARM_UP_S = 6.0  # the longest a Helm warm-up turn is waited for
WARM_UP_UNSEEN_S = 2.0  # a warm-up never seen busy counts as over after this long at rest


def _wait_for_open_streams(api: RuntimeApi, timeout: float = STREAM_OPEN_WAIT_S) -> tuple[int, int, float]:
    """Hold the prompt until the workspace stream of every session the cell already has is subscribed.

    The stream reader finds a session by listing the host and then connects, on its own threads. A fixed pause before the prompt
    lost that race about one run in four on a loaded machine (2026-11-12: three of thirteen Codex Helm subagent runs): the subagent
    start was published 50 ms before the stream subscribed, to no subscriber, and the cell read "the host does not publish it".
    The host does not replay previews to a first connection, so a frame published before the subscription is gone for that client.
    ``RuntimeApi.subscribed_streams`` holds a session once its stream delivered the first ``workspace_changed``, which the host
    sends from inside its subscription. Returns (streams subscribed, sessions known, seconds waited); a cell with no session yet (a
    cold Helm launch) has nothing to wait for and returns at once.
    """

    started = time.monotonic()
    wanted = set(api.session_ids())
    while wanted - api.subscribed_streams and time.monotonic() - started < timeout:
        time.sleep(0.02)
    return len(wanted & api.subscribed_streams), len(wanted), time.monotonic() - started


def _rel(value: float | None, base: float) -> float | None:
    return None if value is None else round(value - base, 3)


def _enable_claude_thinking(pas: Pass) -> None:
    """Claude Code sends a `thinking` request parameter only when asked; the scripted model returns a thinking block only to such a request."""

    for penv in pas.provider_environments.values():
        config = penv.get("CLAUDE_CONFIG_DIR")
        if not config:
            continue
        settings = Path(config) / "settings.json"
        current = json.loads(settings.read_text()) if settings.exists() else {}
        current["alwaysThinkingEnabled"] = True
        settings.write_text(json.dumps(current))


def _enable_codex_multi_agent(pas: Pass) -> None:
    """Codex offers no subagent tool unless its ``multi_agent`` feature is on; turn it on in the harness HOME's config only."""

    config = pas.home / ".codex" / "config.toml"
    text = config.read_text() if config.exists() else ""
    if "multi_agent" in text:
        return
    text = (
        text.replace("[features]\n", "[features]\nmulti_agent = true\n", 1)
        if "[features]" in text
        else text + "\n[features]\nmulti_agent = true\n"
    )
    config.write_text(text)


def _session_facts(api: RuntimeApi) -> list[dict[str, Any]]:
    """Every session on the host (hidden ones too) with the fields that say how it relates to the others."""

    rows: list[dict[str, Any]] = []
    for path in (
        "/api/agents/sessions?limit=100&include_test=true",
        "/api/agents/sessions?limit=100&include_test=true&include_hidden=true",
    ):
        with contextlib.suppress(Exception):
            status, body = api.get(path)
            if status == 200:
                for item in json.loads(body).get("sessions") or []:
                    rows.append(item)
    seen: dict[str, dict[str, Any]] = {}
    for item in rows:
        seen[str(item.get("id"))] = item
    return list(seen.values())


def run_kind_cell_in(
    pas: Pass,
    plan: KindPlan,
    *,
    ready_timeout: float,
    drivers: dict[str, ProviderDriver],
    expand_key: bool = True,
    old_client: bool = False,
    warm_session: bool = False,
    observer: Any = None,
) -> dict[str, Any]:
    """``observer`` (optional) is a second client of the same turn, such as a real browser: ``before_submit(host_url, session_ids, plan)``
    runs after the lanes are open and just before the prompt goes, ``turn_over()`` when the turn's last durable event has been seen and
    the harness is about to stop, and ``finish()`` returns what it saw (stored under ``out["observer"]``) before the host is torn down."""
    spec = plan.spec
    provider, mode = spec.provider, spec.mode
    driver, binary = drivers[provider], pas.binaries[provider]
    started = time.monotonic()
    spans = Spans()
    out: dict[str, Any] = {
        "cell": spec.key,
        "provider": provider,
        "mode": mode,
        "scenario": spec.scenario,
        "provider_version": _version(binary),
        "engine_version": _version(str(pas.engine)),
        "nonce": plan.nonce,
    }
    if plan.not_measured:
        out.update(outcome="not_measured", reason=plan.not_measured)
        return out
    spans.mark("versions")
    obs = KindObservation(plan)
    workspace = pas.root / "w" / provider / f"{mode}-{plan.nonce}"
    workspace.mkdir(parents=True)
    api = RuntimeApi(pas.host.url, pas.host.token, workspace=workspace, device_id=pas.host.device_id)
    api.legacy_stream = old_client
    stop = threading.Event()
    session: ProviderPtySession | None = None
    recording, timing_file = (
        pas.root / f"terminal-{provider}-{mode}-{spec.scenario}.raw",
        pas.root / f"terminal-{provider}-{mode}-{spec.scenario}.timing",
    )
    watchers: list[Any] = []
    files: FileWatcher | None = None
    side: SidePoller | None = None
    markers = plan.markers
    submitted = started
    key = EXPAND_KEYS.get((provider, spec.scenario)) if (expand_key and mode != "console") else None
    anchor_marker = plan.group(ANCHOR_GROUP[spec.scenario])[0].marker
    # Read after the try: a cell that fails before the prompt has neither.
    streams_open, streams_wanted, stream_wait_s = 0, 0, 0.0
    launched_listed = True
    woken_worst: dict[str, Any] | None = None
    try:
        if mode != "console":
            if pas.cli is None:
                raise RuntimeError("the native longhouse CLI is not installed on this machine")
            session = ProviderPtySession.start(
                argv=_helm_argv(pas.cli, provider, workspace, binary, pas.proxy.url, pas.host.token),
                cwd=workspace,
                env=pas.provider_environments[provider],
                terminal_path=recording,
                timing_path=timing_file,
                rows=ROWS,
                columns=COLUMNS,
            )
            obs.ready_evidence = _wait_ready(session, recording, timing_file, driver.ready_text, ready_timeout, driver.answers)
        prompt_ready_at = time.monotonic()
        spans.mark("provider ready")
        if warm_session and session is not None:
            _warm_helm_session(session, api, plan.nonce)
            out["warm_up_s"] = round(time.monotonic() - prompt_ready_at, 2)
            spans.mark("warm-up turn")
        watchers = [LaneWatcher(lane, markers, stop) for lane in api.poll_lanes()]
        stream = FramingStreamWatcher("workspace stream (SSE)", lambda: api.stream_lines(stop), markers, stop)
        stream.prompt = plan.prompt
        watchers.append(stream)
        state_observer = StateObserver(api, stop)
        woken_reader = WokenStateReader(api, stop)
        api.on_wake = woken_reader.wake
        console_session = ""
        if mode == "console":
            created = _create_console_session(api, provider, pas.shipper.machine_name, workspace)
            console_session = str(created["session_id"])
            api.pinned_sessions = [console_session]
            obs.ready_evidence = f"console session {console_session} created; adapter available"
        files = FileWatcher(pas.home, markers, stop, only_under=TRANSCRIPT_ROOTS.get(provider, ()))
        files.scan_once()
        side_markers = [s.marker for s in plan.signals if s.group in plan.side_file_groups]
        if side_markers:
            side = SidePoller(workspace / SIDE_FILE, side_markers, stop)
        probe = SubagentProbe(api, [s.marker for s in plan.group("subagent_text")], stop) if spec.scenario == "subagent" else None
        for watcher in watchers:
            watcher.start()
        state_observer.start()
        woken_reader.start()
        files.start()
        if side:
            side.start()
        if probe:
            probe.start()
        time.sleep(
            0.5
        )  # the facts lane reads a few idle baselines before the prompt: a state it shows later is the scenario's, not left over
        launched_listed = api.wait_for_launched_session(STREAM_OPEN_WAIT_S)
        streams_open, streams_wanted, stream_wait_s = _wait_for_open_streams(api)
        if observer is not None:
            observer.before_submit(pas.host.url, api.session_ids(), plan)
        prompted_sessions = set(api.known_sessions)
        spans.mark("session listed, lanes open")
        submitted = time.monotonic()
        if mode == "console":
            api.post(
                f"/api/agents/sessions/{console_session}/turns", {"message": plan.prompt, "client_request_id": f"tt-{secrets.token_hex(4)}"}
            )
        else:
            assert session is not None
            session.submit_line(plan.prompt)
        # The scenario is over when the scripted model has written everything it was scripted to write and the durable
        # events carry the end of a message: the parent's reply, or the child's last words. Either, because not both
        # always come: Codex's parent answers before its child finishes, and Claude's child text is never served, so
        # waiting for that one marker held every Claude subagent cell to its 72 s deadline (2026-11-13).
        last_markers = {group[-1].marker for group in (plan.group("subagent_text"), plan.group("reply")) if group}
        model_markers = [s.marker for s in plan.signals if s.group in plan.model_groups]
        events_lane = next(w for w in watchers if isinstance(w, LaneWatcher) and w.lane.name.startswith("events"))
        deadline = submitted + plan.max_seconds
        done_at: float | None = None
        seen_count, seen_changed_at = -1, submitted

        def everything_seen() -> int:
            count = sum(len(w.first_seen) for w in watchers) + len(files.first_seen) + (len(side.first_seen) if side else 0)
            return count + (len(probe.children) + len(probe.child_marker_first) if probe else 0)

        while time.monotonic() < deadline:
            now = time.monotonic()
            if (count := everything_seen()) != seen_count:
                seen_count, seen_changed_at = count, now
            if key and obs.press_t is None and session is not None:
                anchored = pas.model.sent_times([anchor_marker])[anchor_marker]
                if anchored is not None and now >= anchored + PRESS_AFTER_ANCHOR_S[spec.scenario]:
                    obs.press_t = now
                    session.write(key)
            if done_at is None and last_markers & set(events_lane.first_seen) and None not in pas.model.sent_times(model_markers).values():
                done_at = now
                spans.mark("prompt to last event")
            if done_at is not None:
                served = [sid for sid in prompted_sessions if sid not in state_observer.gone]
                if now >= seen_changed_at + AT_REST_HOLD_S and turn_at_rest(
                    list(state_observer.timeline), list(woken_reader.timeline), served, now
                ):
                    out["left_on"] = "turn at rest"
                    break
                if now > done_at + SETTLE_S:
                    out["left_on"] = "settle time"
                    break
            if session is not None and not session.alive():
                out["left_on"] = "provider exited"
                break
            time.sleep(0.2)
        spans.mark("after last event" if done_at is not None else "prompt to deadline")
        if observer is not None:
            observer.turn_over()
            out["observer"] = observer.finish()
        stop.set()
        stopped_at = time.monotonic()
        for watcher in watchers:
            watcher.join(timeout=5)
        state_observer.join(timeout=5)
        woken_reader.join(timeout=5)
        files.join(timeout=5)
        if side:
            side.join(timeout=5)
        spans.mark("lanes closed")
        if probe:
            probe.join(timeout=5)
            out["subagent_probe"] = {
                "delegation_first_nonzero_s": _rel(probe.delegation_first, submitted),
                "delegation_first_value": probe.delegation_first_value,
                "delegation_last_value": probe.delegation_last_value,
                "children_first_listed_s": _rel(probe.children_first, submitted),
                "children": {c: {**v, "first_listed": _rel(v["first_listed"], submitted)} for c, v in probe.children.items()},
                "child_text_first_s": {m: _rel(t, submitted) for m, t in probe.child_marker_first.items()},
                "child_text_session": probe.child_marker_session,
                "errors": probe.errors,
            }
        obs.model_sent = pas.model.sent_times(markers)
        if mode != "console":
            chunks = load_timed_chunks(recording, timing_file)
            obs.terminal_any = terminal_first_seen(chunks, markers)
            if obs.press_t is not None:
                obs.terminal_default = terminal_first_seen([c for c in chunks if c.t < obs.press_t], markers)
            else:
                obs.terminal_default = dict(obs.terminal_any)
        obs.side = dict(side.first_seen) if side else {}
        for watcher in watchers:
            obs.lanes[watcher.lane.name] = dict(watcher.first_seen)
        obs.provider_file = dict(files.first_seen)
        obs.state_timeline, obs.state_reads, obs.state_shaped, obs.submitted = (
            list(state_observer.timeline),
            state_observer.reads,
            state_observer.shaped,
            submitted,
        )
        out["state_lane"] = state_lane_result(obs, stopped_at)
        out["state_lane"]["errors"] = state_observer.errors
        out["state_lane"]["resolution_s"] = state_observer.resolution_s
        woken_lags = woken_view_lags(
            state_observer.timeline, woken_reader.timeline, since=submitted, end_t=stopped_at, gone=state_observer.gone
        )
        # Judged on the sessions the prompt went to. A subagent's child session appears mid-turn and its stream opens when the harness
        # finds it, so its states are recorded below and not gated.
        woken_worst = max((lag for lag in woken_lags if lag["session_id"] in prompted_sessions), key=lambda lag: lag["lag_s"], default=None)
        out["state_lane"]["woken_view"] = {
            "wakes": woken_reader.wakes,
            "reads": woken_reader.reads,
            "errors": woken_reader.errors,
            "states": [
                {
                    "t": round(lag["served_at"] - submitted, 2),
                    "prompted_session": lag["session_id"] in prompted_sessions,
                    **{k: lag[k] for k in ("headline", "detail", "served_for_s", "shown", "lag_s")},
                }
                for lag in woken_lags
            ][:80],
            "max_lag_s": woken_worst["lag_s"] if woken_worst else None,
        }
        out["engine_runtime_samples"] = [
            {**x, "t": round(x["t"] - submitted, 3)} for x in pas.proxy.runtime_samples if set(x["markers"]) & set(markers)
        ][:400]
        wanted = set(markers)
        obs.engine_runtime = {m: t for m, t in pas.proxy.runtime_posts.items() if m in wanted}
        obs.engine_other = {m: t for m, t in pas.proxy.other_posts.items() if m in wanted}
        out["sse_frames"] = [{**f, "t": round(f["t"] - submitted, 3)} for f in stream.frames]
        out["sse_event_counts"] = stream.event_counts
        # A frame published before the stream subscribed went to nobody: the host does not replay previews to a first connection.
        out["stream_open_before_prompt"] = {
            "open": streams_open,
            "sessions": streams_wanted,
            "waited_s": round(stream_wait_s, 3),
            "launched_session_listed": launched_listed,
            "launched_session_waited_s": None if api.launched_listed_s is None else round(api.launched_listed_s, 3),
        }
        out["lane_errors"] = {w.lane.name: w.errors for w in watchers if w.errors}
        out["file_scan_gap_s"] = files.resolution_s
        out["file_paths"] = sorted(set(files.first_path.values()))
        publishes, longest, runtime_requests = host_publishes(pas.host.log_path, api.known_sessions)
        out["host_publish_lines"] = publishes
        out["host_runtime_events_requests_pass_total"] = runtime_requests
        out["sessions_after"] = _session_facts(api)
        out["cell_sessions"] = sorted(api.known_sessions)
        out["events_by_session"] = _events_by_session(api, plan)
    except Exception as exc:  # noqa: BLE001 - becomes a failed preflight check carrying the cause
        obs.preflight_error = f"{type(exc).__name__}: {exc}"
        if recording.exists():
            out["last_output"] = _tail_text(recording)
    finally:
        stop.set()
        spans.mark("read back")
        if session is not None:
            session.close()
        spans.mark("provider stopped")
    receipt = _served_per_turn(pas, plan)
    obs.turns_served = receipt
    checks = judge_kinds(obs)
    if woken_worst is not None and woken_worst["lag_s"] > WOKEN_STATE_BUDGET_S:
        # The poller sees every state the host serves; a client sees only what it is woken to read.
        shown = "showed it" if woken_worst["shown"] else "never showed it"
        checks.append(
            (
                WOKEN_STATE_CHECK,
                False,
                f"the host served {woken_worst['headline']!r} for {woken_worst['served_for_s']:.1f} s and a woken-only client {shown} "
                f"({woken_worst['lag_s']:.1f} s behind, budget {WOKEN_STATE_BUDGET_S:.1f} s)",
            )
        )
    if not launched_listed:
        # The live frames go to the launched session; a cell that only ever saw the file's session would blame the host.
        checks.append(
            (
                "the launched session was listed before the prompt",
                False,
                f"only a session discovered from the provider's file was listed after {STREAM_OPEN_WAIT_S:.0f} s",
            )
        )
    if streams_open < streams_wanted:
        # Without this the stream lane would read "absent" and blame the host for a frame no client was attached to receive.
        checks.append(
            (
                "the workspace stream was subscribed before the prompt",
                False,
                f"{streams_open} of {streams_wanted} session streams subscribed after {stream_wait_s:.1f} s",
            )
        )
    out["checks"] = [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks]
    out["outcome"] = "measured" if all(ok for _, ok, _ in checks) else "fail"
    if out["outcome"] == "fail":
        out["reason"] = "; ".join(f"{n}: {d}" for n, ok, d in checks if not ok)
    out["expand_key"] = key.hex() if key else None
    out["press_after_s"] = _rel(obs.press_t, submitted)
    out["model_requests_per_turn"] = receipt
    out["signals"] = _signal_rows(obs, submitted, mode)
    spans.mark("judged")
    out["timing"] = {
        "spans": spans.to_json(),
        "prepare_home_s": pas.prepared.get(provider),
        **{f"pass_{name}": seconds for name, seconds in pas.timing.items()},
    }
    out["wall_s"] = round(time.monotonic() - started, 1)
    return out


def _served_per_turn(pas: Pass, plan: KindPlan) -> dict[str, int]:
    return {tid: pas.model.served_requests([tid]) for tid in plan.turn_ids}


def _events_by_session(api: RuntimeApi, plan: KindPlan) -> dict[str, Any]:
    """For each session the cell can see: which signal groups its durable events carry, and the event shape that carried the first of each."""

    result: dict[str, Any] = {}
    for sid in sorted(api.known_sessions):
        with contextlib.suppress(Exception):
            status, body = api.get(f"/api/agents/sessions/{sid}/events?limit=1000")
            if status != 200:
                continue
            events = json.loads(body).get("events") or []
            groups: dict[str, Any] = {}
            for event in events:
                text = json.dumps(event)
                for signal in plan.signals:
                    if signal.marker in text and signal.group not in groups:
                        groups[signal.group] = {
                            k: (v if not isinstance(v, str) else v[:80])
                            for k, v in event.items()
                            if k in {"role", "tool_name", "content_type", "kind", "event_type", "type"}
                        }
            result[sid] = {"events": len(events), "groups": groups}
    return result


def _signal_rows(obs: KindObservation, submitted: float, mode: str) -> list[dict[str, Any]]:
    rows = []
    sse = "workspace stream (SSE)"
    events = next((n for n in obs.lanes if n.startswith("events")), "")
    workspace = next((n for n in obs.lanes if n.startswith("workspace")), "")
    for s in obs.plan.signals:
        m = s.marker
        model = obs.model_sent.get(m)
        side = obs.side.get(m)
        term = obs.terminal_any.get(m)
        default = obs.terminal_default.get(m)
        ref = side if s.group in obs.plan.side_file_groups and side is not None else (model if mode == "console" else term)
        row = {
            "name": s.name,
            "group": s.group,
            "marker": m,
            "model_sent": _rel(model, submitted),
            "side_file": _rel(side, submitted),
            "terminal_any": _rel(term, submitted),
            "terminal_default_view": _rel(default, submitted),
            "provider_file": _rel(obs.provider_file.get(m), submitted),
            "engine_runtime_post": _rel(obs.engine_runtime.get(m), submitted),
            "engine_other_post": _rel(obs.engine_other.get(m), submitted),
            "sse": _rel(obs.lanes.get(sse, {}).get(m), submitted),
            "events": _rel(obs.lanes.get(events, {}).get(m), submitted),
            "workspace": _rel(obs.lanes.get(workspace, {}).get(m), submitted),
            "reference": _rel(ref, submitted),
        }
        rows.append(row)
    return rows


def group_summary(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per signal group: how many reached each stage and the first and median delay against the reference."""

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["group"], []).append(row)
    summary: dict[str, dict[str, Any]] = {}
    for name, items in groups.items():
        entry: dict[str, Any] = {"n": len(items)}
        for stage in ("terminal_any", "terminal_default_view", "provider_file", "engine_runtime_post", "sse", "events", "workspace"):
            seen = [r[stage] for r in items if r[stage] is not None]
            delays = [r[stage] - r["reference"] for r in items if r[stage] is not None and r["reference"] is not None]
            entry[stage] = {
                "seen": len(seen),
                "first_s": min(seen) if seen else None,
                "delay_median_s": round(statistics.median(delays), 2) if delays else None,
            }
        refs = [r["reference"] for r in items if r["reference"] is not None]
        entry["reference_first_s"] = min(refs) if refs else None
        entry["reference_last_s"] = max(refs) if refs else None
        summary[name] = entry
    return summary


def stops_at(entry: dict[str, Any], mode: str) -> str:
    """The first stage, going down the live path, at which a group's markers are missing."""

    n = entry["n"]
    if entry["reference_first_s"] is None:
        return "provider_emitted (no baseline time)"
    for stage, label in (("engine_runtime_post", "engine_posted"), ("sse", "stream_delivered")):
        if entry[stage]["seen"] < n:
            return f"{label} ({entry[stage]['seen']} of {n})" if entry[stage]["seen"] else label
    return "-"


def run_kinds_suite(
    specs: Sequence[CellSpec],
    *,
    engine: Path,
    longhouse_cli: Path | None = None,
    keep: bool = False,
    ready_timeout: float = 120.0,
    drivers: dict[str, ProviderDriver] | None = None,
    expand_key: bool = True,
    mutation: str | None = None,
    host_environment: dict[str, str] | None = None,
    old_client: bool = False,
    warm_session: bool = False,
    observer_factory: Callable[[CellSpec], Any] | None = None,
    log: Callable[[str], None] = lambda _m: None,
    text_before_tool: bool = False,
    engine_delay_ms: int = 0,
) -> list[dict[str, Any]]:
    """Run each scenario cell. OMP gets a pass of its own (it shares a config layout with Pi); the rest share one pass.

    ``engine_delay_ms`` holds every chunk between the engine and the host that long, each way, as a network to a hosted tenant
    would; ``text_before_tool`` is ``plan_kind_cell``'s. Together they are the conditions under which a call's start shares
    a post with, or lands behind, newer reply text.
    """

    import shutil

    drivers = drivers or PROVIDERS
    cli = longhouse_cli or (Path(shutil.which("longhouse")) if shutil.which("longhouse") else None)
    results: list[dict[str, Any]] = []
    plans = {(s.key, s.scenario): plan_kind_cell(s, mutation, text_before_tool=text_before_tool) for s in specs}
    runnable = [s for s in specs if not plans[(s.key, s.scenario)].not_measured]
    for s in specs:
        if plans[(s.key, s.scenario)].not_measured:
            results.append(
                {
                    "cell": s.key,
                    "provider": s.provider,
                    "mode": s.mode,
                    "scenario": s.scenario,
                    "outcome": "not_measured",
                    "reason": plans[(s.key, s.scenario)].not_measured,
                }
            )
    for apart in (False, True):
        group = [s for s in runnable if (s.provider in HOME_EXCLUSIVE) == apart]
        if not group:
            continue
        # build_pass keys its turns by cell key, so one pass holds one scenario of each cell: split by scenario.
        for scenario in sorted({s.scenario for s in group}):
            part = [s for s in group if s.scenario == scenario]
            keyed = {s.key: plans[(s.key, s.scenario)] for s in part}
            try:
                pas = build_pass(part, keyed, engine=engine, cli=cli, host_environment=host_environment, log=log, log_all_frames=True)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001
                for s in part:
                    results.append(
                        {
                            "cell": s.key,
                            "provider": s.provider,
                            "mode": s.mode,
                            "scenario": scenario,
                            "outcome": "fail",
                            "reason": f"pass setup failed: {type(exc).__name__}: {exc}",
                        }
                    )
                continue
            pas.proxy.delay_s = max(0, engine_delay_ms) / 1000.0
            try:
                if scenario == "thinking":
                    _enable_claude_thinking(pas)
                if scenario == "subagent":
                    _enable_codex_multi_agent(pas)
                for s in part:
                    log(f"running {s.key} {scenario}")
                    results.append(
                        run_kind_cell_in(
                            pas,
                            keyed[s.key],
                            ready_timeout=ready_timeout,
                            drivers=drivers,
                            expand_key=expand_key,
                            old_client=old_client,
                            warm_session=warm_session,
                            observer=observer_factory(s) if observer_factory else None,
                        )
                    )
            finally:
                stop_pass(pas, keep)
    return results


# ---- report ------------------------------------------------------------------------------------------------------------


def _f(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


# Where a live signal stops when the harness saw neither the Longhouse stream nor an engine runtime post carry it.
# (provider, mode, group) -> (the provider has a live source for it, what that source is, what drops it today).
# From the 2026-11 provider audits (docs/archive/2026-11-provider-live-surface-audit) and the code they cite; a dated citation, the run's own
# evidence (an engine runtime post, a stream frame) always wins over this table.
LIVE_SOURCE: dict[tuple[str, str, str], tuple[bool, str, str]] = {
    ("claude", "console", "thinking"): (True, "stream-json thinking_delta (--include-partial-messages, already on)", "host"),
    ("claude", "console", "tool_started"): (True, "stream-json tool_use block and system task_started", "host"),
    ("claude", "console", "tool_output"): (False, "no stream carries a running tool's stdout (audit claude.md)", ""),
    ("claude", "console", "subagent_started"): (True, "stream-json tool_use Agent and system task_started", "host"),
    ("claude", "console", "subagent_text"): (True, "--forward-subagent-text (whole child messages)", "engine: the flag is not passed"),
    ("claude", "helm", "thinking"): (True, "mod turn.step thinking chunk", "mod: register.js liveTurnStep"),
    ("claude", "helm", "tool_started"): (
        True,
        "PreToolUse hook; mod turn.step tool chunk",
        "mod: register.js liveTurnStep",
    ),
    ("claude", "helm", "tool_output"): (False, "no hook or mod event carries a running tool's stdout", ""),
    ("claude", "helm", "subagent_started"): (
        True,
        "PreToolUse/SubagentStart hooks",
        "mod: register.js liveTurnStep",
    ),
    ("claude", "helm", "subagent_text"): (False, "no hook or TUI event carries child text", ""),
    ("codex", "console", "thinking"): (True, "app-server item/reasoning/*Delta", "host"),
    ("codex", "helm", "thinking"): (
        True,
        "app-server item/reasoning/summaryTextDelta",
        "bridge: BRIDGE_OPT_OUT_NOTIFICATION_METHODS opts out",
    ),
    ("codex", "helm", "tool_started"): (
        True,
        "app-server item/started commandExecution",
        "bridge: extract_live_transcript_delta keeps agentMessage only",
    ),
    ("codex", "helm", "tool_output"): (True, "app-server item/commandExecution/outputDelta", "bridge: counted as activity, text dropped"),
    ("opencode", "console", "thinking"): (
        True,
        "server SSE message.part.delta on a reasoning part",
        "engine: opencode_run posts no reasoning delta (none on the wire in the run)",
    ),
    ("opencode", "console", "tool_output"): (
        True,
        "server SSE tool part state.metadata.output",
        "engine: forwards a tool part at status changes only",
    ),
    ("opencode", "console", "subagent_text"): (True, "child session SSE", "provider: child writes at its own end (measured 14.2 s of 15)"),
    ("opencode", "helm", "thinking"): (
        True,
        "/global/event message.part.delta on a reasoning part",
        "bridge: LiveTextTracker (opencode_bridge.rs) returns None for any part not declared text",
    ),
    ("opencode", "helm", "tool_started"): (
        True,
        "/global/event message.part.updated tool part (pending, running)",
        "bridge: LiveTextTracker reads text parts only; tool parts ship from the DB when they end",
    ),
    ("opencode", "helm", "tool_output"): (
        True,
        "/global/event tool part state.metadata.output; DB tool part running",
        "bridge: LiveTextTracker reads text parts only; tool parts ship from the DB when they end",
    ),
    ("opencode", "helm", "subagent_started"): (True, "/global/event tool part (task)", "bridge: tool parts ship from the DB when they end"),
    ("opencode", "helm", "subagent_text"): (
        True,
        "child session on /global/event",
        "bridge: LiveTextTracker follows this session only; child text lands in the DB at its end",
    ),
    ("pi", "console", "thinking"): (
        True,
        "RPC message_update thinking_delta",
        "engine: console_rpc is_non_text_rpc_message_update strips thinking_* and toolcall_* frames before they are posted",
    ),
    ("pi", "helm", "thinking"): (True, "extension message_update thinking_delta", "extension: forwards text_delta only"),
    ("pi", "helm", "tool_started"): (True, "extension tool_execution_start", "extension: emits phase only"),
    ("pi", "helm", "tool_output"): (True, "extension tool_execution_update partialResult (cumulative)", "extension: emits phase only"),
    ("omp", "console", "thinking"): (
        True,
        "RPC message_update thinking_delta",
        "engine: omp_print.rs drops thinking_* and toolcall_* message_update frames (console_rpc is_non_text_rpc_message_update)",
    ),
    ("omp", "console", "tool_started"): (True, "print-mode tool_execution_start", "host"),
    ("omp", "console", "tool_output"): (True, "print-mode tool_execution_update partialResult", "host"),
    ("omp", "console", "subagent_started"): (True, "print-mode tool_execution_start (task)", "host"),
    ("omp", "console", "subagent_text"): (True, "print-mode tool_execution_update on the task tool", "host"),
    ("omp", "helm", "thinking"): (True, "extension message_update thinking_delta", "extension: lifecycle() keeps text_delta only"),
    ("omp", "helm", "tool_started"): (True, "extension tool_execution_start", "extension: lifecycle event carries phase only"),
    ("omp", "helm", "tool_output"): (
        True,
        "extension tool_execution_update partialResult",
        "extension: lifecycle event carries phase only",
    ),
    ("omp", "helm", "subagent_started"): (True, "extension tool_execution_start (task)", "extension: lifecycle event carries phase only"),
    ("omp", "helm", "subagent_text"): (
        True,
        "extension tool_execution_update on the task tool; child session file",
        "extension; child file read at its end",
    ),
    ("antigravity", "console", "thinking"): (False, "agy -p stdout has no thought field; agy acp has agent_thought_chunk (not used)", ""),
    ("antigravity", "console", "tool_started"): (True, "stdout step_update tool ACTIVE", "engine: read_print_output drops step_update"),
    ("antigravity", "console", "tool_output"): (False, "a foreground command's output exists only at completion", ""),
    ("antigravity", "helm", "thinking"): (
        True,
        "conversation db: the streaming model step's thought field",
        "engine: antigravity_helm_live thinking",
    ),
    ("antigravity", "helm", "tool_started"): (
        True,
        "PreToolUse hook; conversation db tool-call step",
        "engine: antigravity_helm_live tool",
    ),
    ("antigravity", "helm", "tool_output"): (False, "a foreground command's output exists only at completion", ""),
}


def _group_class(entry: dict[str, Any], stage: str) -> str:
    """live / partial / late / absent / no_baseline for one stage of one signal group against the signal's own reference times."""

    seen, first = entry[stage]["seen"], entry[stage]["first_s"]
    first_ref, last_ref = entry["reference_first_s"], entry["reference_last_s"]
    if first_ref is None:
        return "no_baseline" if seen == 0 else "seen_no_baseline"
    if seen == 0:
        return "absent"
    end = last_ref if entry["n"] > 1 else first_ref + 2.0  # one-marker signals: within 2 s of the signal; streams: before the signal ends
    if first is not None and first > end:
        return "late"
    return "live" if seen >= entry["n"] else "partial"


def reclassify(cell: dict[str, Any], group: str, entry: dict[str, Any]) -> str:
    """Where this signal's live path ends, in the four-way split: delivered, engine posts and host drops, provider emits and our code drops, no live source."""

    sse = _group_class(entry, "sse")
    if sse in {"live", "partial"}:
        return "delivered" if sse == "live" else "delivered (partial)"
    posted = _group_class(entry, "engine_runtime_post")
    if posted in {"live", "partial"}:
        return (
            "engine posts it; the host does not publish it"
            if cell["mode"] == "console"
            else "engine posts it; the host does not publish it"
        )
    known = LIVE_SOURCE.get((cell["provider"], cell["mode"], group))
    if known is None:
        return "unclassified (no engine post, no audit row)"
    has_source, source, dropper = known
    if not has_source:
        return f"provider has no live source ({source})"
    return f"provider emits it ({source}); {dropper or 'our code'} drops it"


def _state_cell(cell: dict[str, Any]) -> str:
    lane = cell.get("state_lane") or {}
    verdict = lane.get("verdict") or {}
    klass = verdict.get("class", "-")
    if klass in {"live", "late"}:
        shown = "; ".join(verdict.get("shown") or [])[:70]
        return (
            f"{klass} (up at +{_f(verdict.get('lag_s'))} s, {int(round((verdict.get('coverage') or 0) * 100))}% of window) shows: {shown}"
        )
    if klass == "absent":
        return "absent (never shown in the window)" + (f"; saw: {'; '.join(verdict.get('shown') or [])}" if verdict.get("shown") else "")
    return f"{klass}: {verdict.get('reason', '')}".strip()


def _content_cell(entry: dict[str, Any], stage: str) -> str:
    st = entry[stage]
    klass = _group_class(entry, stage)
    if st["seen"] == 0:
        return klass
    lag = f", median lag {_f(st['delay_median_s'])} s" if st["delay_median_s"] is not None else ""
    return f"{klass} {st['seen']}/{entry['n']} (first {_f(st['first_s'])} s{lag})"


def render_kinds(results: Sequence[dict[str, Any]]) -> str:
    """One table per scenario. STATE is the facts lane (what the runtime strip draws); CONTENT is the transcript preview (SSE) and the durable events."""

    lines: list[str] = []
    for scenario in SCENARIOS:
        cells = [r for r in results if r.get("scenario") == scenario]
        if not cells:
            continue
        lines += [
            f"### {scenario} (one run per cell; times are seconds after the prompt was sent)",
            "",
            "| cell | lane | signal | terminal | Longhouse state facts | content: live preview (SSE) | content: durable events | where the live path ends |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for cell in cells:
            if cell["outcome"] != "measured":
                lines.append(f"| {cell['cell']} | | | | | | | {cell['outcome']}: {cell.get('reason', '')[:160]} |")
                continue
            lines.append(f"| {cell['cell']} | state | {scenario} in progress | | {_state_cell(cell)} | | | |")
            for group, entry in group_summary(cell["signals"]).items():
                if group == "reply":
                    continue
                term = entry["terminal_any"]
                tdef = entry["terminal_default_view"]
                terminal = (
                    "console"
                    if cell["mode"] == "console"
                    else f"{term['seen']}/{entry['n']} first {_f(term['first_s'])} ({tdef['seen']} before key)"
                )
                lines.append(
                    f"| {cell['cell']} | content | {group} | {terminal} | | {_content_cell(entry, 'sse')} | {_content_cell(entry, 'events')} | {reclassify(cell, group, entry)} |"
                )
        lines.append("")
    return "\n".join(lines)


# ---- what a published live item must look like (the six cells the host used to drop) -------------------------------------------

# (cell, scenario) -> (signal group that must reach the stream live, the preview kind it must carry). Before 2026-11-11 the host published
# none of these: it allowlisted a few `progress_kind` values and hard-coded the tool name `exec`.
KIND_EXPECTATIONS: dict[tuple[str, str], tuple[str, str]] = {
    ("claude/console", "thinking"): ("thinking", "thinking"),
    ("codex/console", "thinking"): ("thinking", "thinking"),
    ("claude/console", "tool"): ("tool_started", "tool"),
    ("omp/console", "tool"): ("tool_output", "tool"),
    ("claude/console", "subagent"): ("subagent_started", "subagent"),
    ("omp/console", "subagent"): ("subagent_started", "subagent"),
    # Engine-side forwards (2026-11-12): the Claude mod's turn.step chunks. Run with --warm-session.
    ("claude/helm", "thinking"): ("thinking", "thinking"),
    ("claude/helm", "tool"): ("tool_started", "tool"),
    ("claude/helm", "subagent"): ("subagent_started", "subagent"),
    ("antigravity/helm", "thinking"): ("thinking", "thinking"),
    ("antigravity/helm", "tool"): ("tool_started", "tool"),
    ("omp/helm", "thinking"): ("thinking", "thinking"),
    ("omp/helm", "tool"): ("tool_started", "tool"),
    ("omp/helm", "subagent"): ("subagent_started", "subagent"),
    ("pi/helm", "thinking"): ("thinking", "thinking"),
    ("pi/helm", "tool"): ("tool_started", "tool"),
    # Console thinking (2026-11-12): the engine posts it through the status slot beside the stripped stream event.
    ("omp/console", "thinking"): ("thinking", "thinking"),
    ("pi/console", "thinking"): ("thinking", "thinking"),
    # Codex (2026-11-12): the bridge forwards reasoning, tool and subagent items; Console names its tools. Run Helm with --warm-session.
    ("codex/helm", "thinking"): ("thinking", "thinking"),
    ("codex/helm", "tool"): ("tool_started", "tool"),
    ("codex/console", "tool"): ("tool_output", "tool"),
    ("codex/helm", "subagent"): ("subagent_started", "subagent"),
    ("codex/console", "subagent"): ("subagent_started", "subagent"),
    # OpenCode Helm (2026-11-12): reasoning, tool and task parts from /global/event. Run with --warm-session.
    ("opencode/helm", "thinking"): ("thinking", "thinking"),
    ("opencode/helm", "tool"): ("tool_started", "tool"),
    ("opencode/helm", "subagent"): ("subagent_started", "subagent"),
    ("opencode/console", "thinking"): ("thinking", "thinking"),
    ("opencode/console", "tool"): ("tool_output", "tool"),
    ("opencode/console", "subagent"): ("subagent_started", "subagent"),
}


# Cells where a served state shown late is known and reported, not failed: cell -> why. Everywhere else a late state fails.
# Read by the all-checks run (terminal_truth_parallel); the mutation tests have their own rule for it (judge_unmutated).
KNOWN_LATE_STATE: dict[str, str] = {
    "opencode/console": "OpenCode Console can take over 15 s to its first model output on a loaded machine; the host then changes "
    "'Thinking' to 'Last observed thinking' by expiry and sends no frame, so a woken-only client shows the old state for about "
    "2 s (control-plane docs/specs/terminal-truth-ci-gate.md, 'Mutation tests on the Linux runner', observation 1). Not fixed.",
}


def kinds_expectation(result: dict[str, Any]) -> dict[str, Any] | None:
    """Did this cell's live item reach the stream, with the right kind, and where did it stop if not.

    The stage is the first one down the path that lost it: ``engine_posted`` (the engine's own post never carried it),
    ``host_published`` (the engine posted it live and the stream never carried it: the host dropped it), ``stream_delivered`` (it arrived
    only after the signal ended), ``kind`` (it arrived without the right kind, or a tool under the name ``exec``).
    """

    key = (result.get("cell"), result.get("scenario"))
    want = KIND_EXPECTATIONS.get(key)  # type: ignore[arg-type]
    if want is None or result.get("outcome") != "measured":
        return None
    group, kind = want
    entry = group_summary(result["signals"]).get(group)
    if entry is None:
        return None
    posted, sse = _group_class(entry, "engine_runtime_post"), _group_class(entry, "sse")
    # A signal the terminal never paints (OpenCode reasoning) has no reference time, so "live" cannot be judged against it.
    # Every marker arriving on the stream under the right kind is then the pass; none arriving still fails at its stage.
    if posted == "seen_no_baseline" and entry["engine_runtime_post"]["seen"] >= entry["n"]:
        posted = "live"
    if sse == "seen_no_baseline" and entry["sse"]["seen"] >= entry["n"]:
        sse = "live"
    frames = [f for f in result.get("sse_frames", []) if f.get("kind") == kind]
    stage = "-"
    if posted not in {"live", "partial"}:
        stage = "engine_posted"
    elif sse == "absent":
        stage = "host_published"
    elif sse not in {"live", "partial"}:
        stage = "stream_delivered"
    elif not frames or (kind == "tool" and not any(f.get("tool_name") not in (None, "", "exec") for f in frames)):
        stage = "kind"
    # Reasoning must not be on the page as the reply, whatever else arrived. The stream lane counts a marker in a frame of any
    # kind, so reasoning previewed as reply text beside one correct thinking frame read as delivered (OpenCode 1.19.5 on the
    # Linux runner, 2026-11-13: the mutated projector sent the deltas as reply text and the part snapshot as thinking).
    markers = [row["marker"] for row in result["signals"] if row["group"] == group] if kind == "thinking" else []
    as_reply = [
        f for f in result.get("sse_frames", []) if f.get("kind") == "reply" and any(m in str(f.get("text_tail") or "") for m in markers)
    ]
    if stage == "-" and as_reply:
        stage = "kind"
    return {
        "cell": result["cell"],
        "scenario": result["scenario"],
        "group": group,
        "expected_kind": kind,
        "engine_posted": posted,
        "stream": sse,
        "stream_first_s": entry["sse"]["first_s"],
        "stream_median_lag_s": entry["sse"]["delay_median_s"],
        "kinds_on_stream": sorted({str(f.get("kind")) for f in result.get("sse_frames", [])}),
        "tool_names_on_stream": sorted({str(f["tool_name"]) for f in result.get("sse_frames", []) if f.get("tool_name")}),
        "thinking_markers_in_reply_frames": len(as_reply),
        "stage": stage,
        "ok": stage == "-",
    }


def kinds_verdict(result: dict[str, Any]) -> tuple[str, str | None, str]:
    """(outcome, first failed stage, reason) of one live-kinds cell, judged by its published-kind check.

    ``pass``: the scenario happened, its live item reached the stream under the right kind and every served state was shown.
    ``fail`` names the first stage down the path that lost it: ``preflight`` (the scenario did not happen), one of
    ``kinds_expectation``'s stages, or ``state_shown`` (the host served a state and woke nobody). ``measured``: the cell ran
    and has no published-kind expectation. ``not_measured``: the provider has no such thing to run.
    """

    if result["outcome"] == "not_measured":
        return "not_measured", None, str(result.get("reason", ""))
    if result["outcome"] != "measured":
        failed = [check["name"] for check in result.get("checks", []) if not check["ok"]]
        reason = str(result.get("reason", ""))
        if failed != [WOKEN_STATE_CHECK]:
            return "fail", "preflight", reason
        # The one failure that is about the product rather than the run: the host served a state and woke nobody. It is
        # the last stage down the path. A live item that was lost or mislabelled upstream wakes nobody either, so when
        # the published-kind check also fails, that stage is the first failure and the unshown state its consequence
        # (codex-helm-thinking-as-reply on the runner, 2026-11-12: reasoning sent as the reply, "Thinking" never shown).
        upstream = kinds_expectation({**result, "outcome": "measured"})
        if upstream is not None and not upstream["ok"]:
            return "fail", upstream["stage"], f"{json.dumps({k: upstream[k] for k in ('engine_posted', 'stream')})}; then {reason}"
        intact = "" if upstream is None else f" (published-kind check ok: kinds on the stream {upstream.get('kinds_on_stream')})"
        return "fail", "state_shown", f"{reason}{intact}"
    check = kinds_expectation(result)
    if check is None:
        return "measured", None, "the cell has no published-kind expectation"
    return (
        ("pass" if check["ok"] else "fail"),
        (None if check["ok"] else check["stage"]),
        json.dumps({k: check[k] for k in ("engine_posted", "stream")}),
    )


def render_expectations(results: Sequence[dict[str, Any]]) -> str:
    rows = [r for r in (kinds_expectation(x) for x in results) if r]
    if not rows:
        return ""
    lines = [
        "### published kind check (the six cells the host used to drop)",
        "",
        "| cell | scenario | signal | expected kind | engine posted | stream | first (s) | median lag (s) | kinds on stream | tool names | stopped at | ok |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['cell']} | {r['scenario']} | {r['group']} | {r['expected_kind']} | {r['engine_posted']} | {r['stream']} | {_f(r['stream_first_s'])} | "
            f"{_f(r['stream_median_lag_s'])} | {', '.join(r['kinds_on_stream']) or '-'} | {', '.join(r['tool_names_on_stream']) or '-'} | {r['stage']} | {'yes' if r['ok'] else 'NO'} |"
        )
    return "\n".join(lines)
