"""Mutation tests for the terminal-truth gate: each reintroduces one defect fixed on 2026-11-11 and must fail its cell.

A gate that has never been shown to fail proves little. Each mutation here is a one-place source edit that puts back one
of the defects the harness found, a cell that the defect breaks, and the stage where the verdict must say the text stopped.
(The OpenCode part-type mutation of 2026-11-11 was retired on 2026-11-12: once the bridge published reasoning as its own
thinking preview, that write replaced the mislabelled one in the status slot before it was posted, so the edit no longer
broke anything; ``opencode-helm-thinking-as-reply`` reintroduces the same defect, reasoning shown as the reply, and fails.) The command runs the unmutated cell first (it must pass), then the
mutated one (it must fail at the named stage), and exits non-zero on any mismatch. When the unmutated cell fails, the row
is a miss already, so its mutation is not built or run; it is run once more first, and the first failure stays on the row
(``RETRIED``). An unmutated cell whose one fault is a served state shown late does not stop a mutation about another stage:
the mutation is judged and the late state is printed as a ``FINDING``, which does not change the exit status. Every row that
is not ok is printed again under the table with the harness's reason (``NO``), and ``--out`` holds the rows as JSON, rewritten
after each one, with what the failing live-kinds cell saw (checks, states served and shown) under ``evidence``.

    make terminal-truth-mutations ARGS="--engine <engine> --longhouse-cli <longhouse>"

Server-side mutations shadow ``zerg`` with a patched copy for the scratch Runtime Host only. Engine mutations are applied in
a scratch git worktree and rebuilt (the build reuses a clone of this checkout's cargo target, so only the engine crates
recompile). Two further checks need no source edit: a reworded ready string must fail preflight, and a fake model
streaming at half speed must still pass.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from zerg.qa import terminal_truth as tt
from zerg.qa.scripted_model_server import parse_pacing
from zerg.qa.terminal_truth_kinds import WOKEN_STATE_CHECK
from zerg.qa.terminal_truth_kinds import kinds_expectation
from zerg.qa.terminal_truth_kinds import run_kinds_suite

REPO_ROOT = tt.REPO_ROOT
# What a live-kinds cell's result says about a failure: the checks, the states served and shown, and how the run ended.
EVIDENCE_KEYS = ("checks", "state_lane", "stream_open_before_prompt", "lane_errors", "sse_event_counts", "last_output", "wall_s")
SCRATCH = tt.SCRATCH_ROOT.parent / "tt-mut"


@dataclasses.dataclass(frozen=True)
class Mutation:
    name: str
    defect: str
    side: str  # server | engine
    file: str  # repo-relative
    old: str  # must occur exactly once
    new: str
    cell: str  # provider/mode the defect breaks
    stages: tuple[str, ...]  # the verdict must name one of these as the first failed stage
    scenario: str = (
        ""  # thinking | tool | subagent: judge the cell with the live-kinds scenario and its published-kind check, not reply text
    )
    warm: bool = False  # live-kinds scenarios on a Helm cell: open the session and its stream before the scenario prompt (--warm-session)
    text_before_tool: bool = False  # tool scenario: reply text is streamed right before the tool call (--text-before-tool)
    engine_delay_ms: int = 0  # hold engine<->host bytes this long each way, as a hosted tenant's network does (--engine-delay-ms)

    @property
    def run_options(self) -> dict[str, Any]:
        return {"warm_session": self.warm, "text_before_tool": self.text_before_tool, "engine_delay_ms": self.engine_delay_ms}


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "pi-helm-text-forward",
        "drop the Pi Helm extension's forwarding of reply text (2a7854633)",
        "engine",
        "engine/assets/longhouse-pi-helm.ts",
        'if (update?.type === "text_delta" && typeof update.delta === "string" && update.delta) {',
        "if (false) {",
        "pi/helm",
        ("engine_posted",),
    ),
    Mutation(
        "claude-helm-text-forward",
        "drop the Claude Helm mod's forwarding of reply text from the turn.step stream",
        "engine",
        "engine/assets/claude-mod/hooks/register.js",
        "if (chunk.kind === 'text' && typeof chunk.text === 'string' && chunk.text !== '') {",
        "if (false) {",
        "claude/helm",
        ("engine_posted",),
    ),
    Mutation(
        "claude-helm-thinking-forward",
        "drop the Claude Helm mod's forwarding of thinking chunks from the turn.step stream",
        "engine",
        "engine/assets/claude-mod/hooks/register.js",
        "} else if (chunk.kind === 'thinking' && typeof chunk.text === 'string' && chunk.text !== '') {",
        "} else if (false) {",
        "claude/helm",
        ("engine_posted",),
        scenario="thinking",
        warm=True,
    ),
    Mutation(
        "claude-helm-thinking-as-reply",
        "the Claude Helm relay posts thinking text without saying it is thinking (the host would show it as the reply)",
        "engine",
        "engine/src/claude_helm_live.rs",
        '                    kind: Some("thinking"),\n                    tool_name: None,',
        "                    kind: None,\n                    tool_name: None,",
        "claude/helm",
        ("kind", "stream_delivered", "host_published"),
        scenario="thinking",
        warm=True,
    ),
    Mutation(
        "claude-helm-tool-forward",
        "drop the Claude Helm mod's forwarding of tool-call starts and arguments from the turn.step stream",
        "engine",
        "engine/assets/claude-mod/hooks/register.js",
        "chunk.kind === 'tool' &&\n        typeof chunk.id === 'string' &&",
        "false &&\n        typeof chunk.id === 'string' &&",
        "claude/helm",
        ("engine_posted",),
        scenario="tool",
        warm=True,
    ),
    Mutation(
        "claude-helm-subagent-forward",
        "drop the Claude Helm mod's forwarding of subagent (Agent tool) starts from the turn.step stream",
        "engine",
        "engine/assets/claude-mod/hooks/register.js",
        "chunk.kind === 'tool' &&\n        typeof chunk.id === 'string' &&",
        "false &&\n        typeof chunk.id === 'string' &&",
        "claude/helm",
        ("engine_posted",),
        scenario="subagent",
        warm=True,
    ),
    Mutation(
        "antigravity-helm-thinking-forward",
        "the Antigravity Helm reader stops publishing the streaming step's thought field",
        "engine",
        "engine/src/antigravity_helm_live.rs",
        "            if let Some(mut preview) = current.thinking.observe(idx, status, thinking) {",
        "            if let Some(mut preview) = current.thinking.observe(idx, status, thinking.filter(|_| false)) {",
        "antigravity/helm",
        ("engine_posted",),
        scenario="thinking",
        warm=True,
    ),
    Mutation(
        "antigravity-helm-thinking-as-reply",
        "the Antigravity Helm reader posts the thought text without saying it is thinking (the host would show it as the reply)",
        "engine",
        "engine/src/antigravity_helm_live.rs",
        '                preview.kind = Some("thinking");',
        "                preview.kind = None;",
        "antigravity/helm",
        ("kind", "stream_delivered", "host_published"),
        scenario="thinking",
        warm=True,
    ),
    Mutation(
        "antigravity-helm-tool-forward",
        "the Antigravity Helm reader stops publishing tool-call steps from the conversation db",
        "engine",
        "engine/src/antigravity_helm_live.rs",
        "                previews.extend(current.tools.observe(idx, status, tool_call(&payload)));",
        "                previews.extend(current.tools.observe(idx, status, tool_call(&payload).filter(|_| false)));",
        "antigravity/helm",
        ("engine_posted",),
        scenario="tool",
        warm=True,
    ),
    Mutation(
        "omp-helm-thinking-forward",
        "the OMP Helm launcher stops publishing the extension's thinking deltas",
        "engine",
        "engine/src/omp_helm_launcher.rs",
        "if let Some(delta) = omp_thinking_delta(event) {",
        "if let Some(delta) = omp_thinking_delta(event).filter(|_| false) {",
        "omp/helm",
        ("engine_posted",),
        scenario="thinking",
        warm=True,
    ),
    Mutation(
        "omp-helm-thinking-as-reply",
        "the OMP Helm launcher posts thinking text without saying it is thinking (the host would show it as the reply)",
        "engine",
        "engine/src/omp_helm_launcher.rs",
        'kind: Some("thinking".into()),',
        "kind: None,",
        "omp/helm",
        ("kind", "stream_delivered", "host_published"),
        scenario="thinking",
        warm=True,
    ),
    Mutation(
        "omp-helm-tool-forward",
        "the OMP Helm launcher stops publishing the extension's tool_execution frames as tool previews",
        "engine",
        "engine/src/omp_helm_launcher.rs",
        "let Some(event) = event else { return };",
        "let Some(event) = event.filter(|_| false) else { return };",
        "omp/helm",
        ("engine_posted",),
        scenario="tool",
        warm=True,
    ),
    Mutation(
        "pi-helm-thinking-forward",
        "the Pi Helm launcher stops publishing the extension's thinking deltas",
        "engine",
        "engine/src/pi_helm_launcher.rs",
        "if let Some(delta) = pi_thinking_delta(&frame) {",
        "if let Some(delta) = pi_thinking_delta(&frame).filter(|_| false) {",
        "pi/helm",
        ("engine_posted",),
        scenario="thinking",
        warm=True,
    ),
    Mutation(
        "pi-helm-thinking-as-reply",
        "the Pi Helm launcher posts thinking text without saying it is thinking (the host would show it as the reply)",
        "engine",
        "engine/src/pi_helm_launcher.rs",
        'kind: Some("thinking".into()),',
        "kind: None,",
        "pi/helm",
        ("kind", "stream_delivered", "host_published"),
        scenario="thinking",
        warm=True,
    ),
    Mutation(
        "pi-helm-tool-forward",
        "the Pi Helm launcher stops publishing the extension's tool_call frames as tool previews",
        "engine",
        "engine/src/pi_helm_launcher.rs",
        '"tool_call" => self.publish_tool_call(&frame),',
        '"tool_call" => {}',
        "pi/helm",
        ("engine_posted",),
        scenario="tool",
        warm=True,
    ),
    Mutation(
        "omp-console-thinking-forward",
        "OMP Console stops collecting the thinking deltas it strips from the posted stream event",
        "engine",
        "engine/src/omp_print.rs",
        "projection.thinking.observe(seq, &event);",
        "let _ = seq;",
        "omp/console",
        ("engine_posted",),
        scenario="thinking",
        warm=True,
    ),
    Mutation(
        "omp-console-thinking-as-reply",
        "the Console engine posts thinking text without saying it is thinking (the host would show it as the reply)",
        "engine",
        "engine/src/console_sink.rs",
        'kind: Some("thinking".into()),',
        "kind: None,",
        "omp/console",
        ("kind", "stream_delivered", "host_published"),
        scenario="thinking",
        warm=True,
    ),
    Mutation(
        "pi-console-thinking-forward",
        "Pi Console stops collecting the thinking deltas it strips from the posted stream event",
        "engine",
        "engine/src/pi_print.rs",
        "projection.thinking.observe(*seq, &event);",
        "let _ = seq;",
        "pi/console",
        ("engine_posted",),
        scenario="thinking",
        warm=True,
    ),
    Mutation(
        "pi-console-thinking-as-reply",
        "the Console engine posts thinking text without saying it is thinking (the host would show it as the reply)",
        "engine",
        "engine/src/console_sink.rs",
        'kind: Some("thinking".into()),',
        "kind: None,",
        "pi/console",
        ("kind", "stream_delivered", "host_published"),
        scenario="thinking",
        warm=True,
    ),
    Mutation(
        "codex-helm-thinking-forward",
        "the Codex Helm bridge stops posting the app-server's reasoning summary deltas",
        "engine",
        "engine/src/codex_bridge.rs",
        "post_live_thinking(context, method, &params);",
        "let _ = (&context, method, &params);",
        "codex/helm",
        ("engine_posted",),
        scenario="thinking",
        warm=True,
    ),
    Mutation(
        "codex-helm-thinking-as-reply",
        "the Codex Helm bridge posts reasoning text as reply text (the host would show it as the reply)",
        "engine",
        "engine/src/codex_bridge.rs",
        "post_live_thinking(context, method, &params);",
        'if let Some(delta) = params.get("delta").and_then(Value::as_str) { let item = "mutation-thinking".to_string(); context.live_transcript_seq += 1; context.live_transcript_text.push_str(delta); context.runtime.post_live_transcript_delta(method, delta, context.state.active_turn_id.as_deref(), &item, context.live_transcript_seq, 1, &context.live_transcript_text).await; }',
        "codex/helm",
        ("kind", "stream_delivered", "host_published"),
        scenario="thinking",
        warm=True,
    ),
    Mutation(
        "codex-helm-tool-forward",
        "the Codex Helm bridge stops posting item/started and item/completed tool calls",
        "engine",
        "engine/src/codex_bridge.rs",
        "post_live_tool_item(context, method, &params);",
        "let _ = (&context, method, &params);",
        "codex/helm",
        ("engine_posted",),
        scenario="tool",
        warm=True,
    ),
    Mutation(
        "codex-console-tool-name",
        "the Codex Console tool post loses the tool's real name (the host would call it exec)",
        "engine",
        "engine/src/codex_exec.rs",
        '"tool_name": tool_name,',
        '"tool_name": Value::Null,',
        "codex/console",
        ("kind",),
        scenario="tool",
    ),
    Mutation(
        "codex-helm-subagent-forward",
        "Codex collaboration (subagent) items stop being posted as live tool events",
        "engine",
        "engine/src/codex_live_kinds.rs",
        '"collabAgentToolCall" => {',
        '"collabAgentToolCall" => {\n            return None;',
        "codex/helm",
        ("engine_posted",),
        scenario="subagent",
        warm=True,
    ),
    Mutation(
        "codex-helm-subagent-as-tool",
        "a Codex subagent call keeps the app-server's camelCase name, so the host calls it a plain tool",
        "engine",
        "engine/src/codex_live_kinds.rs",
        "snake_case_tool_name(&tool),",
        "tool.clone(),",
        "codex/helm",
        ("kind",),
        scenario="subagent",
        warm=True,
    ),
    Mutation(
        "codex-console-subagent-forward",
        "Codex Console stops posting a collaboration (subagent) item as a live tool event",
        "engine",
        "engine/src/codex_live_kinds.rs",
        '"collabAgentToolCall" => {',
        '"collabAgentToolCall" => {\n            return None;',
        "codex/console",
        ("host_published",),  # Console also posts the raw app-server notification, so the post lane still sees the spawn text
        scenario="subagent",
    ),
    Mutation(
        "opencode-helm-thinking-forward",
        "the OpenCode Helm bridge stops publishing reasoning parts",
        "engine",
        "engine/src/opencode_bridge.rs",
        'let key = format!("{part_id}:thinking");',
        'return None;\n        let key = format!("{part_id}:thinking");',
        "opencode/helm",
        ("engine_posted",),
        scenario="thinking",
        warm=True,
    ),
    Mutation(
        "opencode-helm-thinking-as-reply",
        "the OpenCode Helm bridge publishes reasoning text without saying it is thinking (the host would show it as the reply)",
        "engine",
        "engine/src/opencode_bridge.rs",
        'kind: Some("thinking"),',
        "kind: None,",
        "opencode/helm",
        ("kind", "stream_delivered", "host_published"),
        scenario="thinking",
        warm=True,
    ),
    Mutation(
        "opencode-helm-tool-forward",
        "the OpenCode Helm bridge stops posting tool parts as tool previews",
        "engine",
        "engine/src/opencode_bridge.rs",
        "let outbox = crate::config::get_agent_runtime_events_outbox_dir()?;",
        "return Ok(());\n    let outbox = crate::config::get_agent_runtime_events_outbox_dir()?;",
        "opencode/helm",
        ("engine_posted",),
        scenario="tool",
        warm=True,
    ),
    Mutation(
        "opencode-helm-subagent-as-tool",
        "the OpenCode Helm bridge posts a task (subagent) part as a plain tool",
        "engine",
        "engine/src/opencode_bridge.rs",
        "kind: Some(crate::status_slot::tool_preview_kind(&tool.name).into()),",
        'kind: Some("tool".into()),',
        "opencode/helm",
        ("kind",),
        scenario="subagent",
        warm=True,
    ),
    Mutation(
        "opencode-console-thinking-forward",
        "the OpenCode Console projector stops carrying reasoning deltas",
        "engine",
        "engine/src/opencode_run.rs",
        "crate::status_slot::append_live_text(text, delta);",
        "let _ = delta;",
        "opencode/console",
        ("engine_posted",),
        scenario="thinking",
    ),
    Mutation(
        "opencode-console-thinking-as-reply",
        "the OpenCode Console projector previews reasoning deltas as reply text (the bug the reasoning guard fixed)",
        "engine",
        "engine/src/opencode_run.rs",
        'Some("text") => true,\n                    Some("reasoning") => {',
        'Some("text") | Some("reasoning") => true,\n                    Some("never") => {',
        "opencode/console",
        ("kind", "stream_delivered", "host_published"),
        scenario="thinking",
    ),
    Mutation(
        "opencode-console-tool-output-forward",
        "the OpenCode Console projector forwards a tool part at status changes only, so running output never streams",
        "engine",
        "engine/src/opencode_run.rs",
        'running_tool_output(part).filter(|_| status == "running")',
        "running_tool_output(part).filter(|_| false)",
        "opencode/console",
        ("engine_posted",),
        scenario="tool",
    ),
    Mutation(
        "opencode-host-publish",
        "the host stops publishing OpenCode's nested live text (c510accaf)",
        "server",
        "server/zerg/routers/runtime.py",
        '(event.provider or "").strip().lower() in {"pi", "omp", "opencode"}',
        '(event.provider or "").strip().lower() in {"pi", "omp"}',
        "opencode/console",
        ("host_published",),
    ),
    Mutation(
        "omp-session-dir-rule",
        "the launcher picks ~/.local/share/omp when XDG_DATA_HOME is unset, as before 125833163",
        "engine",
        "engine/src/omp_session.rs",
        'if let Some(xdg_data_home) = nonempty_env_path("XDG_DATA_HOME") {',
        '{ let xdg_data_home = std::env::var_os("XDG_DATA_HOME").map(PathBuf::from).filter(|value| !value.as_os_str().is_empty()).unwrap_or_else(|| home_dir().join(".local").join("share"));',
        "omp/helm",
        ("preflight", "engine_posted"),
    ),
    Mutation(
        "antigravity-step-update-arm",
        "Antigravity Console ignores stream-json step_update records (cdda35f88)",
        "engine",
        "engine/src/antigravity_print.rs",
        'if record.get("event").and_then(Value::as_str) != Some("step_update") {\n            return None;\n        }',
        "if true {\n            return None;\n        }",
        "antigravity/console",
        ("engine_posted",),
    ),
    Mutation(
        "antigravity-helm-reader",
        "the Antigravity Helm conversation-database reader is switched off",
        "engine",
        "engine/src/antigravity_helm_live.rs",
        "const READER_ENABLED: bool = true;",
        "const READER_ENABLED: bool = false;",
        "antigravity/helm",
        ("engine_posted",),
    ),
    Mutation(
        "host-live-item-drop",
        "the host stops publishing Claude Console's tool and subagent items (it allowlisted a few progress kinds and drew no other live item)",
        "server",
        "server/zerg/services/session_live_previews.py",
        'if provider == "claude" and source.lower() == "claude_print" and progress_kind == "claude_print_stream":\n        return _claude_print_item_candidate(',
        "if False:\n        return _claude_print_item_candidate(",
        "claude/console",
        ("host_published",),
        scenario="tool",
    ),
    Mutation(
        "host-thinking-drop",
        "the host stops publishing Codex Console reasoning (no candidate for the app-server's item/reasoning deltas)",
        "server",
        "server/zerg/services/session_live_previews.py",
        'if provider == "codex" and source.lower() == "codex_app_server" and progress_kind == "codex_app_server_jsonrpc":\n        return _codex_reasoning_candidate(',
        "if False:\n        return _codex_reasoning_candidate(",
        "codex/console",
        ("host_published",),
        scenario="thinking",
    ),
    Mutation(
        "heartbeat-state-change-not-announced",
        "a heartbeat that changes a session's served state wakes nobody again: the page keeps the label it read at the "
        "runtime event (Antigravity Helm said Thinking through a tool run and after the turn had ended)",
        "server",
        "server/zerg/routers/heartbeat.py",
        "        _publish_state_changes(result, commit_seq=commit_seq, exact_replay=exact_replay)\n",
        "        pass\n",
        "antigravity/helm",
        ("state_shown",),
        scenario="tool",
        warm=True,
    ),
    Mutation(
        "host-call-ordered-against-reply",
        "the host keeps one preview per session in each post again, the one with the highest seq: behind 150 ms of network, "
        "Antigravity's tool start shares a post with newer reply text and is not shown until the tool has finished",
        "server",
        "server/zerg/routers/runtime.py",
        '        key = (sid, preview_lane(candidate) if candidate is not None else "")\n'
        "        existing = latest_by_lane.get(key)\n"
        "        if existing is not None:\n"
        "            if candidate is not None and existing[2] is not None:\n"
        "                if not candidate_supersedes(candidate, existing[2]):\n"
        "                    continue\n"
        "            elif _preview_seq(preview) < _preview_seq(existing[1]):\n"
        "                continue\n",
        '        key = (sid, "")\n'
        "        existing = latest_by_lane.get(key)\n"
        "        if existing is not None:\n"
        "            if _preview_seq(preview) < _preview_seq(existing[1]):\n"
        "                continue\n",
        "antigravity/helm",
        ("host_published", "stream_delivered"),
        scenario="tool",
        warm=True,
        text_before_tool=True,
        engine_delay_ms=150,
    ),
)


def mutated_text(mutation: Mutation, text: str) -> str:
    """The file with the mutation applied; refuses unless the edit lands in exactly one place."""

    if text.count(mutation.old) != 1:
        raise RuntimeError(f"mutation {mutation.name}: the text to mutate occurs {text.count(mutation.old)} times in {mutation.file}")
    return text.replace(mutation.old, mutation.new)


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: float = 1800) -> None:
    subprocess.run(
        command,
        cwd=cwd,
        env=env if env is not None else tt.clean_environ(),
        check=True,
        timeout=timeout,
        stdout=sys.stderr,
        stderr=sys.stderr,
    )


def _build_engine(worktree: Path, target: Path, out: Path) -> tuple[Path, Path]:
    """Build the engine and CLI of ``worktree`` into ``target`` (a clone of a warm cargo target) and copy both binaries to ``out``."""

    rust = Path.home() / ".rustup" / "toolchains" / "1.93.0-aarch64-apple-darwin" / "bin"
    env = tt.clean_environ(
        LONGHOUSE_CARGO_TARGET_DIR=str(target), PATH=f"{rust}{os.pathsep}{os.environ['PATH']}"
    )  # no hook URL or token reaches a build
    _run([sys.executable, "scripts/build/generate_build_identity.py"], cwd=worktree, env=env)
    build = [
        sys.executable, "scripts/build/cargo.py", "exec", "--", "build", "--manifest-path", "engine/Cargo.toml",
        "--profile", "ci-test", "--features", "qa-fault-injection", "--bin", "longhouse-engine", "--bin", "longhouse",
    ]  # fmt: skip
    lock = shutil.which("lockf")
    _run([lock, "/tmp/agents/longhouse-heavy-build.lock", *build] if lock else build, cwd=worktree, env=env)
    out.mkdir(parents=True, exist_ok=True)
    for name in ("longhouse-engine", "longhouse"):
        shutil.copy2(target / "ci-test" / name, out / name)
    return out / "longhouse-engine", out / "longhouse"


def _clone_dir(source: Path, destination: Path) -> None:
    if destination.exists():
        return
    flag = ["-c"] if sys.platform == "darwin" else ["--reflink=auto"]
    if source.exists():
        subprocess.run(["cp", "-R", *flag, str(source), str(destination)], check=True, env=tt.clean_environ())
        (destination / ".longhouse-target.json").unlink(missing_ok=True)  # the clone belongs to the scratch worktree now
    else:
        destination.mkdir(parents=True)


def _verdict(results: list[tt.CellResult]) -> tuple[str, str | None, str]:
    cell = results[0]
    return cell.outcome, cell.first_failed_stage, (cell.reason or "").strip()


def why_not(row: dict[str, Any]) -> str:
    """One line saying why a row is not ok: which of the two cells went wrong, where, and the harness's own reason."""

    if row.get("note"):
        return str(row["note"])
    if row.get("baseline", "pass") != "pass":
        said = f"the unmutated cell came out {row['baseline']} at {row.get('baseline_stage') or '-'}, so the mutated one was not run"
        reason = row.get("baseline_reason")
    else:
        wanted = "a pass" if row["expected_stage"] == "(pass)" else f"a failure at {row['expected_stage']}"
        said = f"expected {wanted}, saw {row.get('observed_outcome')} at {row.get('observed_stage') or '-'}"
        reason = row.get("reason")
    first_line = " ".join(str(reason or "no reason recorded").split())
    return f"{said}: {first_line[:600]}"


def run_mutations(args: argparse.Namespace) -> int:
    pace = parse_pacing(
        {"rate": args.rate, "tokens": args.tokens, "marker_every": 20, "initial_delay": 1.0, "marker_prefix": "TT", "reasoning_tokens": 5}
    )
    expectations = tt.load_expectations()
    base_engine, base_cli = args.engine.resolve(), args.longhouse_cli.resolve()
    rows: list[dict[str, Any]] = []
    last_kinds: dict[str, Any] = {}  # what the most recent live-kinds cell saw, kept on a row that is not ok

    def record(row: dict[str, Any]) -> None:
        """Keep the row, and the file up to date: a run that hits its time limit still leaves every row it finished."""

        if not row["ok"] and "(" in row["cell"] and last_kinds:
            row["evidence"] = dict(last_kinds)
        rows.append(row)
        if args.out:
            args.out.write_text(json.dumps(rows, indent=2) + "\n")

    selected = [m for m in MUTATIONS if not args.only or m.name in args.only]
    SCRATCH.mkdir(parents=True, exist_ok=True)
    worktree = SCRATCH / "worktree"
    engine_built: dict[str, tuple[Path, Path]] = {}

    def kinds_cell(spec: tt.CellSpec, engine: Path, cli: Path, **extra: Any) -> tuple[str, str | None, str]:
        """(outcome, first failed stage, reason) of one live-kinds cell judged by its published-kind check."""

        result = run_kinds_suite([spec], engine=engine, longhouse_cli=cli, **extra)[0]
        last_kinds.clear()
        last_kinds.update({k: result[k] for k in EVIDENCE_KEYS if k in result})
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
            return "fail", "state_shown", reason
        check = kinds_expectation(result)
        if check is None:
            return "fail", "preflight", "the cell has no published-kind expectation"
        return (
            ("pass" if check["ok"] else "fail"),
            (None if check["ok"] else check["stage"]),
            json.dumps({k: check[k] for k in ("engine_posted", "stream")}),
        )

    def cell(spec: tt.CellSpec, engine: Path, cli: Path, **extra: Any) -> tt.CellResult:
        return tt.run_suite([spec], pace=extra.pop("pace", pace), engine=engine, longhouse_cli=cli, expectations=expectations, **extra)[0]

    try:
        if any(m.side == "engine" for m in selected) and not args.skip_engine:
            if not worktree.exists():
                _run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], cwd=REPO_ROOT)
            _clone_dir(Path(args.cargo_target).resolve(), SCRATCH / "target")
        for m in selected:
            spec = tt.CellSpec(*m.cell.split("/"), m.scenario) if m.scenario else tt.CellSpec(*m.cell.split("/"))

            def unmutated() -> tuple[str, str | None, str]:
                return (
                    kinds_cell(spec, base_engine, base_cli, **m.run_options)
                    if m.scenario
                    else _verdict([cell(spec, base_engine, base_cli)])
                )

            def judged_unmutated() -> tuple[str, str | None, str, dict[str, Any] | None]:
                """The unmutated cell as this mutation needs it: (outcome, stage, reason, state finding).

                A mutation is judged on the stage it puts a defect in. When the only thing wrong with the unmutated cell is
                a served state a woken-only client showed late, and the mutation is not about that stage, the item's path
                is intact and the mutation can be judged; the late state is kept as a finding of its own. (Runner,
                2026-11-12: OpenCode Console took 19 s to its first model output, "Thinking" expired at 15 s with no frame,
                and three mutations about reasoning and tool output could not be run.)
                """

                outcome, stage, reason = unmutated()
                if m.scenario and outcome == "fail" and stage == "state_shown" and "state_shown" not in m.stages:
                    return "pass", None, "", {"reason": reason, "evidence": dict(last_kinds)}
                return outcome, stage, reason, None

            baseline_outcome, baseline_stage, baseline_reason, state_finding = judged_unmutated()
            first_failure = ""
            if baseline_outcome != "pass":
                # The unmutated cell is real providers on a shared runner (2026-11-12: Codex's app-server announced no URL
                # once, and a warm-up overlapped the prompt once). One more try; the mutated cell gets none, and the
                # first failure stays on the row.
                first_failure = f"{baseline_stage or '-'}: {baseline_reason}"
                print(f"{m.name}: the unmutated cell failed ({first_failure[:300]}); running it once more", file=sys.stderr)
                baseline_outcome, baseline_stage, baseline_reason, state_finding = judged_unmutated()
            row: dict[str, Any] = {
                "check": f"mutation {m.name}",
                "defect": m.defect,
                "cell": m.cell if not m.scenario else f"{m.cell} ({m.scenario})",
                "expected_stage": "/".join(m.stages),
                "baseline": baseline_outcome,
            }
            if first_failure:
                row.update(baseline_retried=True, baseline_first_failure=first_failure)
            if state_finding:
                row.update(state_finding=state_finding["reason"], state_finding_evidence=state_finding["evidence"])
            if baseline_outcome != "pass":
                # The row is a miss whatever the mutated cell does, and an engine mutation costs a rebuild: keep the reason, skip it.
                row.update(baseline_stage=baseline_stage, baseline_reason=baseline_reason, observed="not run", ok=False)
                record(row)
                continue
            started = time.monotonic()
            if m.side == "server":
                copy = SCRATCH / f"server-{m.name}"
                shutil.rmtree(copy, ignore_errors=True)
                # The host finds config/ relative to the package, so the copy keeps the repository's layout.
                shutil.copytree(tt.SERVER_ROOT / "zerg", copy / "server" / "zerg", ignore=shutil.ignore_patterns("__pycache__"))
                shutil.copytree(REPO_ROOT / "config", copy / "config")
                target_file = copy / "server" / "zerg" / Path(m.file).relative_to("server/zerg")
                target_file.write_text(mutated_text(m, target_file.read_text()))
                if m.scenario:
                    mutated_kinds = kinds_cell(
                        spec, base_engine, base_cli, host_environment={"PYTHONPATH": str(copy / "server")}, **m.run_options
                    )
                else:
                    mutated = cell(spec, base_engine, base_cli, host_environment={"PYTHONPATH": str(copy / "server")})
            elif args.skip_engine:
                row.update(observed="skipped", ok=False, note="--skip-engine")
                record(row)
                continue
            else:
                path = worktree / m.file
                path.write_text(mutated_text(m, path.read_text()))
                try:
                    engine_built[m.name] = _build_engine(worktree, SCRATCH / "target", SCRATCH / f"bin-{m.name}")
                finally:
                    _run(["git", "checkout", "--", "."], cwd=worktree)
                if m.scenario:
                    mutated_kinds = kinds_cell(spec, *engine_built[m.name], **m.run_options)
                else:
                    mutated = cell(spec, *engine_built[m.name])
            outcome, stage, reason = mutated_kinds if m.scenario else _verdict([mutated])
            row.update(observed_outcome=outcome, observed_stage=stage, reason=reason, seconds=round(time.monotonic() - started))
            row["ok"] = outcome == "fail" and stage in m.stages
            record(row)

        if not args.only or "reworded-ready-string" in args.only:
            codex = tt.PROVIDERS["codex"]
            reworded = {**tt.PROVIDERS, "codex": dataclasses.replace(codex, ready_text="Ask Codex to do anything, v2")}
            result = cell(tt.CellSpec("codex", "helm"), base_engine, base_cli, drivers=reworded, ready_timeout=12.0)
            outcome, stage, reason = _verdict([result])
            record(
                {"check": "reworded ready string", "cell": "codex/helm", "expected_stage": "preflight", "observed_outcome": outcome,
                 "observed_stage": stage, "reason": reason, "ok": outcome == "fail" and stage == "preflight"}
            )  # fmt: skip
        if not args.only or "half-speed" in args.only:
            half = dataclasses.replace(pace, rate=pace.rate / 2)
            result = cell(tt.CellSpec("codex", "helm"), base_engine, base_cli, pace=half)
            outcome, stage, reason = _verdict([result])
            record(
                {"check": "half-speed fake model", "cell": "codex/helm", "expected_stage": "(pass)", "observed_outcome": outcome,
                 "observed_stage": stage, "reason": reason, "ok": outcome == "pass"}
            )  # fmt: skip
    finally:
        if worktree.exists():
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=REPO_ROOT, check=False, env=tt.clean_environ())
        shutil.rmtree(SCRATCH, ignore_errors=True)
    print("| check | cell | baseline | expected | observed | stage | ok |")
    print("|---|---|---|---|---|---|---|")
    for row in rows:
        notes = [note for note, on in (("second try", row.get("baseline_retried")), ("state finding", row.get("state_finding"))) if on]
        baseline = f"{row.get('baseline', '-')}{' (' + ', '.join(notes) + ')' if notes else ''}"
        print(
            f"| {row['check']} | {row['cell']} | {baseline} | {row['expected_stage']} | "
            f"{row.get('observed_outcome', row.get('observed'))} | {row.get('observed_stage') or '-'} | {'yes' if row['ok'] else 'NO'} |"
        )
    for row in rows:
        if not row["ok"]:
            print(f"NO {row['check']}: {why_not(row)}")
    # Not misses: the gate caught the defect. They are what the unmutated cells showed on the way, and this run is the
    # only one that puts the live-kinds cells on the Linux runner, so they are printed where the misses are.
    for row in rows:
        if row.get("state_finding"):
            print(f"FINDING {row['cell']}, unmutated: {' '.join(str(row['state_finding']).split())[:600]}")
        if row.get("baseline_retried") and row.get("baseline") == "pass":
            print(f"RETRIED {row['check']}: the unmutated cell failed once at {' '.join(row['baseline_first_failure'].split())[:600]}")
    return 0 if rows and all(row["ok"] for row in rows) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--engine", type=Path, default=Path(os.environ.get("LONGHOUSE_ENGINE_BIN") or shutil.which("longhouse-engine") or "")
    )
    parser.add_argument("--longhouse-cli", type=Path, default=Path(shutil.which("longhouse") or ""))
    parser.add_argument(
        "--cargo-target", type=Path, default=REPO_ROOT / ".build" / "cargo-target", help="warm cargo target to clone for engine rebuilds"
    )
    parser.add_argument("--only", action="append", help="mutation name, 'reworded-ready-string' or 'half-speed'; repeatable")
    parser.add_argument("--skip-engine", action="store_true", help="skip mutations that need an engine rebuild")
    parser.add_argument("--tokens", type=int, default=100)
    parser.add_argument("--rate", type=float, default=20.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if not args.engine.is_file() or not args.longhouse_cli.is_file():
        parser.error("pass --engine and --longhouse-cli (built from this checkout)")
    return run_mutations(args)


if __name__ == "__main__":
    raise SystemExit(main())
