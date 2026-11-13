"""Verdicts and report: stages, the one verdict rule, the ledger's expectations, result records and tables."""

from __future__ import annotations

import json
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from zerg.qa.scripted_model_server import REASONING_WORD
from zerg.qa.terminal_truth_lanes import LaneStats
from zerg.qa.terminal_truth_pty import SERVER_ROOT

STAGES = ("preflight", "provider_emitted", "engine_posted", "host_published", "stream_delivered")


MEDIAN_DELAY_BUDGET_S = (
    0.75  # blocking default; a cell listed in BUDGET_PATH uses its own (derived on the CI runner, arithmetic in the file)
)
BUDGET_PATH = SERVER_ROOT.parent / "schemas" / "terminal-truth-budget.json"


def median_budget_s(provider: str, mode: str) -> float:
    """The blocking median live delay for one cell: its entry in BUDGET_PATH, else MEDIAN_DELAY_BUDGET_S."""

    try:
        cells = json.loads(BUDGET_PATH.read_text()).get("budget_s", {})
    except (OSError, ValueError):
        return MEDIAN_DELAY_BUDGET_S
    value = cells.get(f"{provider}/{mode}")
    return float(value) if isinstance(value, (int, float)) else MEDIAN_DELAY_BUDGET_S


EARLY_FRACTION_MIN = 0.5  # blocking: this share of the markers must reach the client before the provider's last token


# Helm: the median model-write to terminal-paint time. Claude Code paints a line of the reply when the line is complete, so a
# marker (the first word of its line) is painted when the next marker is written: 1.03 s behind at 1.0 s between markers and
# 4.11 s behind at 4.0 s (the nightly's 5 tokens a second, 2026-11-12). The budget is therefore the larger of 4 s and two
# marker spacings; a fixed 4 s failed every slow run of a terminal that was behaving as it always does.
PAINT_LAG_BUDGET_S = 4.0
PAINT_LAG_BUDGET_MARKER_SPACINGS = 2.0


def paint_lag_budget_s(model_sent: Sequence[float]) -> float:
    """The paint-lag budget for a reply whose markers the model wrote at these times."""

    times = sorted(model_sent)
    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
    spacing = statistics.median(gaps) if gaps else 0.0
    return max(PAINT_LAG_BUDGET_S, PAINT_LAG_BUDGET_MARKER_SPACINGS * spacing)


LEDGER_PATH = SERVER_ROOT / "zerg" / "config" / "managed_provider_contracts.json"


@dataclass(frozen=True)
class Expectation:
    """What the provider ledger (``live_stream_signals`` in schemas/managed_providers.yml) says a cell should do."""

    live_reply_text: str  # expected | known_bad | not_measurable | informational
    source: str = ""
    reason: str = ""


def load_expectations(path: Path = LEDGER_PATH) -> dict[tuple[str, str], Expectation]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    found: dict[tuple[str, str], Expectation] = {}
    for item in payload["providers"]:
        for mode, cell in (item.get("live_stream_signals") or {}).items():
            found[(str(item["provider"]), mode)] = Expectation(cell["live_reply_text"], cell.get("source", ""), cell.get("reason", ""))
    return found


@dataclass(frozen=True)
class CellSpec:
    provider: str
    mode: str  # console | helm | shadow
    scenario: str = "long_reply"  # long_reply | two_message

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.mode}"


def declared_cells(expectations: dict[tuple[str, str], Expectation]) -> list[CellSpec]:
    """Every Helm and Console cell the ledger declares, in a stable order."""

    return [CellSpec(p, m) for (p, m) in sorted(expectations, key=lambda k: (k[1] != "console", k[0]))]


@dataclass
class StageResult:
    ok: bool | None  # None: not evaluated (an earlier stage made it meaningless)
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "evidence": self.evidence}


@dataclass
class Observation:
    """Everything the harness saw for one cell, on one monotonic clock, before any judgement."""

    mode: str
    markers: list[str]  # every scheduled marker word, in stream order
    messages: list[str]  # each assistant message the model sends, in order
    preflight_error: str | None = None
    ready_evidence: str = ""
    model_requests_served: int = 0
    reference: dict[str, float | None] = field(default_factory=dict)  # terminal paint (Helm) or model write (Console)
    model_sent: dict[str, float | None] = field(default_factory=dict)
    engine_posted: dict[str, float] = field(default_factory=dict)  # marker -> first runtime-events POST carrying it
    engine_other_posts: int = 0  # markers carried only by other requests (the durable upload)
    host_publishes: int = 0
    host_publish_max_len: int = 0
    stream_first_seen: dict[str, float] = field(default_factory=dict)  # workspace SSE
    previews: dict[str, list[str]] = field(default_factory=dict)
    tool_token: str | None = None  # two-message scenario: text only the tool step carries
    tool_on_client: bool = False
    reasoning_word: str = REASONING_WORD
    scenario: str = "long_reply"
    provider: str = ""  # selects the cell's median budget; empty means the default budget


def preview_segments(texts: Sequence[str]) -> list[list[str]]:
    """Split one session's cumulative previews where a preview does not continue the one before (a new message)."""

    segments: list[list[str]] = []
    for text in texts:
        if segments and text.startswith(segments[-1][-1]):
            segments[-1].append(text)
        else:
            segments.append([text])
    return segments


def check_two_message_previews(previews: dict[str, list[str]], messages: Sequence[str], reasoning_word: str) -> dict[str, Any]:
    """The live preview of a turn with two assistant messages: it shows the first, resets, then shows only the second."""

    if not previews:
        return {"previews": 0}
    session, texts = max(previews.items(), key=lambda item: len(item[1]))
    segments = preview_segments(texts)
    first, second = messages[0].strip(), messages[1].strip()
    return {
        "session": session,
        "previews": len(texts),
        "segments": len(segments),
        "first_message_complete": any(seg[-1] == first for seg in segments),
        "final_equals_second_message": texts[-1] == second,
        "second_starts_fresh": any(seg[0] != "" and not seg[0].startswith(first) and seg[-1] == second for seg in segments),
        "reasoning_leaked": any(reasoning_word in text for text in texts),
    }


def stream_checks(obs: Observation) -> list[tuple[str, bool, str]]:
    """The blocking checks of the stream_delivered stage: (name, ok, detail)."""

    checks: list[tuple[str, bool, str]] = []
    markers = obs.markers
    missing = [m for m in markers if m not in obs.stream_first_seen]
    checks.append(("every_marker_delivered", not missing, f"{len(markers) - len(missing)} of {len(markers)} markers on the live stream"))
    times = [obs.stream_first_seen[m] for m in markers if m in obs.stream_first_seen]
    in_order = all(a <= b for a, b in zip(times, times[1:], strict=False))
    checks.append(
        ("markers_in_order", in_order, "first-seen times nondecreasing along the reply" if in_order else "a later marker arrived first")
    )
    if obs.scenario == "two_message":
        two = check_two_message_previews(obs.previews, obs.messages, obs.reasoning_word)
        ok = (
            bool(two.get("previews"))
            and two.get("first_message_complete")
            and two.get("final_equals_second_message")
            and two.get("second_starts_fresh")
        )
        checks.append(("preview_resets_for_second_message", bool(ok), json.dumps(two)))
        checks.append(("no_reasoning_in_preview", not two.get("reasoning_leaked"), "reasoning words absent from the reply preview"))
        checks.append(("tool_step_on_client", obs.tool_on_client, f"tool token {obs.tool_token} on a client lane"))
    else:
        one = check_live_previews(obs.previews, obs.messages[-1], obs.reasoning_word)
        has = bool(one.get("previews"))
        checks.append(("final_preview_equals_message", has and bool(one.get("final_equals_reply")), json.dumps(one)))
        checks.append(
            (
                "each_preview_continues_the_last",
                has and bool(one.get("each_continues_the_last")),
                f"first break at {one.get('first_break_at_preview')}",
            )
        )
        checks.append(("no_reasoning_in_preview", has and not one.get("reasoning_leaked"), "reasoning words absent from the reply preview"))
    last = obs.reference.get(markers[-1]) if markers else None
    if last is not None:
        early = sum(1 for m in markers if m in obs.stream_first_seen and obs.stream_first_seen[m] < last)
        need = -(-len(markers) // 2)  # at least half, rounded up
        checks.append(
            ("half_delivered_before_last_token", early >= need, f"{early} of {len(markers)} before the provider's last token (need {need})")
        )
    delays = [obs.stream_first_seen[m] - t for m in markers if m in obs.stream_first_seen and (t := obs.reference.get(m)) is not None]
    if delays:
        median = statistics.median(delays)
        budget = median_budget_s(obs.provider, obs.mode) if obs.provider else MEDIAN_DELAY_BUDGET_S
        checks.append(("median_delay_within_budget", median <= budget, f"median {median:.3f} s, budget {budget} s"))
    else:
        checks.append(("median_delay_within_budget", False, "no marker delivered to compare"))
    return checks


def judge(obs: Observation) -> dict[str, StageResult]:
    """The one verdict rule: five stages, each with evidence; the cell's verdict names the first that failed."""

    stages: dict[str, StageResult] = {}
    problem = obs.preflight_error
    if problem is None and not obs.ready_evidence.strip():
        problem = "no readiness evidence (the provider never proved a ready prompt)"
    if problem is None and obs.model_requests_served < 1:
        problem = "the fake model saw no request for this cell"
    stages["preflight"] = StageResult(
        problem is None,
        problem or "",
        {"ready_evidence": obs.ready_evidence[:300], "model_requests_served": obs.model_requests_served},
    )
    if problem is not None:
        for name in STAGES[1:]:
            stages[name] = StageResult(None, "not evaluated: preflight failed")
        return stages

    seen = [m for m in obs.markers if obs.reference.get(m) is not None]
    lags = [r - s for m in seen if (r := obs.reference[m]) is not None and (s := obs.model_sent.get(m)) is not None]
    emitted_problem = ""
    if not seen:
        emitted_problem = "zero markers observed"
    elif len(seen) < len(obs.markers):
        emitted_problem = f"{len(seen)} of {len(obs.markers)} markers observed"
    elif (
        obs.mode == "helm"
        and lags
        and statistics.median(lags)
        > (paint_budget := paint_lag_budget_s([s for m in obs.markers if (s := obs.model_sent.get(m)) is not None]))
    ):
        emitted_problem = (
            f"terminal painted markers {statistics.median(lags):.2f} s after the model wrote them (budget {paint_budget:.1f} s)"
        )
    stages["provider_emitted"] = StageResult(
        not emitted_problem,
        emitted_problem,
        {"markers_observed": len(seen), "markers": len(obs.markers), "paint_lag_median_s": statistics.median(lags) if lags else None},
    )

    posted = [m for m in obs.markers if m in obs.engine_posted]
    posted_problem = "" if len(posted) == len(obs.markers) else f"engine posted live text for {len(posted)} of {len(obs.markers)} markers"
    stages["engine_posted"] = StageResult(
        not posted_problem,
        posted_problem,
        {"markers_in_runtime_posts": len(posted), "markers_only_in_other_posts": obs.engine_other_posts},
    )

    need = len(obs.messages[-1].strip()) if obs.messages else 0
    published_problem = ""
    if obs.host_publishes < 1:
        published_problem = "the host published no live preview for this cell's sessions"
    elif obs.host_publish_max_len < need:
        published_problem = f"the longest live preview the host published had {obs.host_publish_max_len} of {need} characters"
    stages["host_published"] = StageResult(
        not published_problem,
        published_problem,
        {"publishes": obs.host_publishes, "max_text_len": obs.host_publish_max_len, "final_message_len": need},
    )

    checks = stream_checks(obs)
    failed = [f"{name}: {detail}" for name, ok, detail in checks if not ok]
    stages["stream_delivered"] = StageResult(
        not failed,
        "; ".join(failed),
        {"checks": {name: {"ok": ok, "detail": detail} for name, ok, detail in checks}},
    )
    return stages


def first_failed_stage(stages: dict[str, StageResult]) -> str | None:
    return next((name for name in STAGES if stages.get(name) is not None and stages[name].ok is False), None)


def outcome_for(first_failed: str | None, expectation: Expectation, mode: str) -> str:
    """pass | fail | known_bad | known_bad_fixed | not_measurable | measured.

    A cell the ledger declares known-bad is reported as known-bad only when it stops where the defect is (the engine,
    the host or the stream). It failing earlier is a harness or provider problem, and it passing is a stale ledger: both
    fail the run, so a known-bad cell can neither hide a regression nor stay declared bad after it is fixed.
    """

    if mode == "shadow" or expectation.live_reply_text == "informational":
        return "fail" if first_failed == "preflight" else "measured"  # a cell that never ran (or saw no request) measured nothing
    if expectation.live_reply_text == "not_measurable":
        return "not_measurable"
    if expectation.live_reply_text == "known_bad":
        if first_failed is None:
            return "known_bad_fixed"
        return "known_bad" if first_failed in ("engine_posted", "host_published", "stream_delivered") else "fail"
    return "pass" if first_failed is None else "fail"


def exit_code(results: Sequence[Any], declared: Sequence[CellSpec]) -> int:
    """0 only when every declared cell ran and ended pass or known_bad (or not_measurable); anything else is 1."""

    ran = {r.cell for r in results}
    if any(spec.key not in ran for spec in declared):
        return 1
    return 0 if all(r.outcome in {"pass", "known_bad", "not_measurable", "measured"} for r in results) else 1


@dataclass
class CellResult:
    provider: str
    mode: str
    status: str  # "measured" | "could_not_run" (kept for the table); the verdict is ``outcome``
    reason: str | None = None
    scenario: str = "long_reply"
    nonce: str = ""
    outcome: str = "fail"  # pass | fail | known_bad | known_bad_fixed | not_measurable | measured
    first_failed_stage: str | None = None
    stages: dict[str, Any] = field(default_factory=dict)
    expectation: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)  # provider and engine versions: recorded, never judged
    timing: dict[str, Any] = field(default_factory=dict)
    worst_marker: dict[str, Any] = field(default_factory=dict)  # recorded, never blocking
    pass_shape: str = "shared"  # shared | cold
    provider_version: str | None = None
    engine_version: str | None = None
    pace: dict[str, Any] | None = None
    stream_seconds: float | None = None
    markers: list[str] = field(default_factory=list)
    baseline: str | None = None
    terminal_first_seen_s: dict[str, float | None] = field(default_factory=dict)
    model_sent_s: dict[str, float | None] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    file_first_seen_s: dict[str, float | None] = field(default_factory=dict)
    # marker -> where its first SSE frame was in time, seconds after submit: engine_stamp (the event's occurred_at), host_publish
    # (server_fanout_at_ms), sse_yield (server_now_ms when the frame was built), client_receive; recorded, never judged
    stream_stage_s: dict[str, dict[str, float | None]] = field(default_factory=dict)
    file_paths: list[str] = field(default_factory=list)
    file_scan_gap_s: float | None = None
    split: dict[str, Any] = field(default_factory=dict)
    pacing_error_s: float | None = None
    lanes: list[LaneStats] = field(default_factory=list)
    lane_errors: dict[str, list[str]] = field(default_factory=dict)
    stream_sample: list[str] = field(default_factory=list)
    final_preview: dict[str, Any] = field(default_factory=dict)
    live_preview_check: dict[str, Any] = field(default_factory=dict)
    scratch: str | None = None

    @property
    def cell(self) -> str:
        return f"{self.provider}/{self.mode}"

    def to_json(self) -> dict[str, Any]:
        return {
            "cell": self.cell,
            **{k: v for k, v in self.__dict__.items() if k not in {"lanes"}},
            "lanes": [lane.to_json() for lane in self.lanes],
        }


RunResult = CellResult  # the name older callers and tests use


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}s"


def check_live_previews(previews: dict[str, list[str]], expected: str, reasoning_word: str) -> dict[str, Any]:
    """What the live lane showed as reply text, against the reply the model sent.

    Judged on the session with the most previews. Each preview must continue the one before it (cumulative text:
    nothing repeated, reordered or replaced), the last must be the whole reply, and no reasoning word may appear.
    """
    if not previews:
        return {"previews": 0}
    session, texts = max(previews.items(), key=lambda item: len(item[1]))
    broken = next((i for i in range(1, len(texts)) if not texts[i].startswith(texts[i - 1])), None)
    return {
        "session": session,
        "previews": len(texts),
        "each_continues_the_last": broken is None,
        "first_break_at_preview": broken,
        "break_context": [texts[broken - 1][-80:], texts[broken][:80]] if broken else None,
        "final_equals_reply": texts[-1] == expected.strip(),
        "final_length": len(texts[-1]),
        "expected_length": len(expected.strip()),
        "reasoning_leaked": any(reasoning_word in text for text in texts),
    }


def render_table(results: Sequence[RunResult]) -> str:
    lines = [
        "| provider | mode | lane | observations | resolution | first marker | median | worst | on screen, never served |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        if result.status != "measured":
            reason = (result.reason or "").splitlines()[0] if result.reason else ""
            lines.append(f"| {result.provider} | {result.mode} | could not run | | | | | | {reason} |")
            continue
        for lane in result.lanes:
            missing = f"{len(lane.never_served)} of {len(lane.delays)}" + (
                f" ({', '.join(lane.never_served[:4])}{'...' if len(lane.never_served) > 4 else ''})" if lane.never_served else ""
            )
            lines.append(
                f"| {result.provider} {result.provider_version or ''} | {result.mode} | {lane.lane} | {lane.observations} | "
                f"{'stream' if '(SSE)' in lane.lane else ('n/a' if lane.resolution_s is None else f'poll gap {lane.resolution_s * 1000:.0f} ms')} | "
                f"{_fmt(lane.first)} | {_fmt(lane.median)} | {_fmt(lane.worst)} | {missing} |"
            )
    return "\n".join(lines)


def render_final_preview(results: Sequence[RunResult]) -> str:
    """Whether the last live preview a web client received equals the scripted final message, per session."""

    lines = ["| provider | mode | final live preview equals the final message |", "|---|---|---|"]
    for result in results:
        if result.status != "measured":
            continue
        sessions = (result.final_preview or {}).get("sessions") or {}
        if not sessions:
            lines.append(f"| {result.provider} | {result.mode} | no live preview received |")
            continue
        parts = [
            f"{sid[:8]}: {'equal' if info['equals_final_message'] else 'DIFFERENT'} ({info['len']} of {result.final_preview['expected_len']} chars)"
            for sid, info in sessions.items()
        ]
        lines.append(f"| {result.provider} | {result.mode} | {'; '.join(parts)} |")
    return "\n".join(lines)


def render_split(results: Sequence[RunResult]) -> str:
    """Where each durable delay comes from: provider write lag after the last paint, then Longhouse's ship lag."""

    lines = [
        "| provider | mode | stream (first to last marker) | provider wrote last marker, after its last paint | lane | Longhouse ship lag first / median / worst (lane minus provider write) | file watch gap |",
        "|---|---|---|---|---|---|---|",
    ]
    for result in results:
        if result.status != "measured" or not result.split:
            continue
        gap = f"{result.file_scan_gap_s * 1000:.0f} ms" if result.file_scan_gap_s is not None else "-"
        for lane in result.lanes:
            if "SSE" in lane.lane:
                continue
            lines.append(
                f"| {result.provider} | {result.mode} | {_fmt(result.split['stream_s'])} | {_fmt(result.split['provider_write_lag_after_last_paint_s'])} | "
                f"{lane.lane} | {_fmt(lane.ship_first)} / {_fmt(lane.ship_median)} / {_fmt(lane.ship_worst)} | {gap} |"
            )
    return "\n".join(lines)


def render_verdicts(results: Sequence[CellResult]) -> str:
    """One row per cell: the verdict and the first stage where the text stopped."""

    lines = [
        "| cell | verdict | first failed stage | provider | SSE median | worst marker (not blocking) | reason |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        sse = next((lane for lane in r.lanes if "SSE" in lane.lane), None)
        reason = (r.reason or "").splitlines()[0][:160] if r.reason else ""
        lines.append(
            f"| {r.cell} ({r.scenario}) | {r.outcome} | {r.first_failed_stage or '-'} | {r.provider_version or '-'} | "
            f"{_fmt(sse.median if sse else None)} | {_fmt(r.worst_marker.get('delay_s'))} | {reason} |"
        )
    return "\n".join(lines)


def render_timing(results: Sequence[CellResult]) -> str:
    """Where a cell's wall time goes. Boot and teardown of host and engine are a pass's, shared by its cells (shared pass)."""

    keys = [
        ("prepare_home_s", "prepare home"),
        ("pass_boot_host_s", "boot host"),
        ("pass_boot_engine_s", "boot engine"),
        ("provider_start_to_ready_s", "provider start to ready"),
        ("session_listed_after_ready_s", "ready to session listed"),
        ("stream_subscribed_after_ready_s", "ready to stream subscribed"),
        ("stream_s", "stream"),
        ("settle_s", "settle"),
        ("teardown_s", "teardown"),
        ("cell_wall_s", "cell total"),
    ]
    lines = ["| cell | pass | " + " | ".join(label for _, label in keys) + " |", "|---|---|" + "---|" * len(keys)]
    for r in results:
        if not r.timing:
            continue
        lines.append(f"| {r.cell} | {r.pass_shape} | " + " | ".join(_fmt(r.timing.get(k)) for k, _ in keys) + " |")
    return "\n".join(lines)


def summary_json(results: Sequence[CellResult], declared: Sequence[CellSpec], code: int) -> dict[str, Any]:
    ran = {r.cell for r in results}
    return {
        "exit_code": code,
        "declared": [s.key for s in declared],
        "missing": [s.key for s in declared if s.key not in ran],
        "cells": {
            r.cell: {
                "outcome": r.outcome,
                "first_failed_stage": r.first_failed_stage,
                "reason": (r.reason or "")[:400],
                "provider_version": r.provider_version,
                "pass_shape": r.pass_shape,
            }
            for r in results
        },
    }


def write_results(results: Sequence[CellResult], declared: Sequence[CellSpec], code: int, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for r in results:
        (out_dir / f"{r.provider}-{r.mode}-{r.scenario}.json").write_text(json.dumps(r.to_json(), indent=2) + "\n")
    (out_dir / "summary.json").write_text(json.dumps(summary_json(results, declared, code), indent=2) + "\n")
