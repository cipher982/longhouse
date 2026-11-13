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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from zerg.qa import terminal_truth as tt
from zerg.qa.scripted_model_server import parse_pacing
from zerg.qa.terminal_truth_kinds import kinds_verdict
from zerg.qa.terminal_truth_kinds import run_kinds_suite

REPO_ROOT = tt.REPO_ROOT
# What a live-kinds cell's result says about a failure: the checks, the states served and shown, and how the run ended.
EVIDENCE_KEYS = (
    "checks", "state_lane", "stream_open_before_prompt", "lane_errors", "sse_event_counts", "last_output", "wall_s",
    "cell_sessions", "sessions_after",
)  # fmt: skip
# The same for a reply-text cell: where each marker was at each stage, so a late median says which hop was late.
REPLY_EVIDENCE_KEYS = ("pass_shape", "timing", "split", "stream_stage_s", "worst_marker", "diagnostics", "lane_errors", "scratch")
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


BINARIES = ("longhouse-engine", "longhouse")


def _build_env(**extra: str) -> dict[str, str]:
    """The environment of an engine build: no hook URL or token reaches it."""

    rust = Path.home() / ".rustup" / "toolchains" / "1.93.0-aarch64-apple-darwin" / "bin"
    return tt.clean_environ(PATH=f"{rust}{os.pathsep}{os.environ['PATH']}", **extra)


def _build_engine(worktree: Path, target: Path, out: Path) -> tuple[Path, Path]:
    """Build the engine and CLI of ``worktree`` into ``target`` (a clone of a warm cargo target) and copy both binaries to ``out``."""

    env = _build_env(LONGHOUSE_CARGO_TARGET_DIR=str(target))
    _run([sys.executable, "scripts/build/generate_build_identity.py"], cwd=worktree, env=env)
    build = [
        sys.executable, "scripts/build/cargo.py", "exec", "--", "build", "--manifest-path", "engine/Cargo.toml",
        "--profile", "ci-test", "--features", "qa-fault-injection", "--bin", "longhouse-engine", "--bin", "longhouse",
    ]  # fmt: skip
    lock = shutil.which("lockf")
    _run([lock, "/tmp/agents/longhouse-heavy-build.lock", *build] if lock else build, cwd=worktree, env=env)
    out.mkdir(parents=True, exist_ok=True)
    for name in BINARIES:
        shutil.copy2(target / "ci-test" / name, out / name)
    return out / BINARIES[0], out / BINARIES[1]


def _clone_dir(source: Path, destination: Path) -> None:
    if destination.exists():
        return
    flag = ["-c"] if sys.platform == "darwin" else ["--reflink=auto"]
    if source.exists():
        subprocess.run(["cp", "-R", *flag, str(source), str(destination)], check=True, env=tt.clean_environ())
        (destination / ".longhouse-target.json").unlink(missing_ok=True)  # the clone belongs to the scratch worktree now
    else:
        destination.mkdir(parents=True)


# What a mutated engine binary is made from: the engine sources and the two directories its `include_str!` reaches
# (server/tests_lite/test_terminal_truth_mutations.py fails when an include leaves these), the compiler and the build flags.
ENGINE_INPUT_DIRS = ("engine", "config", "schemas")
BUILD_FLAGS = "--profile ci-test --features qa-fault-injection --bin longhouse-engine --bin longhouse"


def engine_inputs_digest(repo_root: Path = REPO_ROOT, flags: str = BUILD_FLAGS) -> str:
    """One hash of everything an engine binary is built from at HEAD (the scratch worktree is a checkout of HEAD).

    The engine sources and what they embed, the build wrapper, the compiler, the build flags and the release version the
    binary reports. Not the commit: a binary built from the same inputs at an earlier commit is the same engine with an
    older build label, which the scratch host records and does not act on.
    """

    tree = subprocess.run(
        ["git", "ls-tree", "-r", "HEAD", "--", *ENGINE_INPUT_DIRS, "scripts/build"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rustc = subprocess.run(["rustc", "--version"], capture_output=True, text=True, check=False, env=_build_env()).stdout.strip()
    version = re.search(r'^version\s*=\s*"([^"]+)"', (repo_root / "server" / "pyproject.toml").read_text(), re.M)
    return hashlib.sha256(f"{tree}\n{rustc}\n{flags}\n{version.group(1) if version else ''}\n".encode()).hexdigest()


def mutated_engine_key(mutation: Mutation, inputs_digest: str) -> str:
    edit = f"{inputs_digest}\0{mutation.file}\0{mutation.old}\0{mutation.new}"
    return hashlib.sha256(edit.encode()).hexdigest()[:24]


def build_mutated_engines(
    selected: Sequence[Mutation], *, cache: Path, cargo_target: Path, log: Callable[[str], None] = lambda _m: None
) -> tuple[dict[str, tuple[Path, Path]], dict[str, Any]]:
    """Every engine mutation's binaries, from ``cache`` when the engine's inputs and the edit are unchanged, built otherwise.

    A mutated binary depends on the engine sources at HEAD and on its one edit, so a push that does not touch the engine
    rebuilds nothing. Returns (name -> (engine, cli), {"hits", "built", "seconds", "inputs_digest"}). Entries of other
    input digests are deleted: the cache holds one engine version.
    """

    started = time.monotonic()
    digest = engine_inputs_digest()
    cache.mkdir(parents=True, exist_ok=True)
    engines: dict[str, tuple[Path, Path]] = {}
    keys = {m.name: mutated_engine_key(m, digest) for m in selected if m.side == "engine"}
    missing = [m for m in selected if m.side == "engine" and not all((cache / keys[m.name] / b).is_file() for b in BINARIES)]
    worktree = SCRATCH / "worktree"
    if missing:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        try:
            if not worktree.exists():
                _run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], cwd=REPO_ROOT)
            _clone_dir(cargo_target.resolve(), SCRATCH / "target")
            for m in missing:
                if all((cache / keys[m.name] / b).is_file() for b in BINARIES):
                    continue  # the same edit as a mutation built a moment ago (one defect, tested on two cells)
                t0 = time.monotonic()
                path = worktree / m.file
                path.write_text(mutated_text(m, path.read_text()))
                staging = cache / f".{keys[m.name]}.{os.getpid()}"
                shutil.rmtree(staging, ignore_errors=True)
                try:
                    _build_engine(worktree, SCRATCH / "target", staging)
                finally:
                    _run(["git", "checkout", "--", "."], cwd=worktree)
                shutil.rmtree(cache / keys[m.name], ignore_errors=True)
                staging.rename(cache / keys[m.name])  # a reader sees a whole entry or none
                log(f"built {m.name} in {time.monotonic() - t0:.0f} s")
        finally:
            if worktree.exists():
                subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=REPO_ROOT, check=False, env=tt.clean_environ())
            shutil.rmtree(SCRATCH, ignore_errors=True)
    for name, key in keys.items():
        engines[name] = (cache / key / BINARIES[0], cache / key / BINARIES[1])
    keep = set(keys.values())
    # Only names this function writes (24 hex digits, or a dot-prefixed staging name) are ever removed, and only when
    # every selected mutation is present: a narrowed run (--only) must not empty the cache of the rest.
    if len(selected) == len(MUTATIONS):
        for entry in cache.iterdir():
            if entry.is_dir() and re.fullmatch(r"\.?[0-9a-f]{24}(\.\d+)?", entry.name) and entry.name not in keep:
                shutil.rmtree(entry, ignore_errors=True)
    built = len({keys[m.name] for m in missing})
    stats = {
        "hits": len(set(keys.values())) - built,
        "built": built,
        "seconds": round(time.monotonic() - started, 1),
        "inputs_digest": digest,
    }
    return engines, stats


def prepare_server_copy(mutation: Mutation, copy: Path) -> Path:
    """A patched copy of the server package for one server-side mutation; returns the PYTHONPATH entry for the scratch host."""

    shutil.rmtree(copy, ignore_errors=True)
    # The host finds config/ relative to the package, so the copy keeps the repository's layout.
    shutil.copytree(tt.SERVER_ROOT / "zerg", copy / "server" / "zerg", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO_ROOT / "config", copy / "config")
    target_file = copy / "server" / "zerg" / Path(mutation.file).relative_to("server/zerg")
    target_file.write_text(mutated_text(mutation, target_file.read_text()))
    return copy / "server"


def judge_unmutated(mutation: Mutation, verdict: tuple[str, str | None, str]) -> tuple[str, str | None, str, str | None]:
    """The unmutated cell as this mutation needs it: (outcome, stage, reason, state finding or None).

    A mutation is judged on the stage it puts a defect in. When the only thing wrong with the unmutated cell is a served
    state a woken-only client showed late, and the mutation is not about that stage, the item's path is intact and the
    mutation can be judged; the late state is kept as a finding of its own. (Runner, 2026-11-12: OpenCode Console took
    19 s to its first model output, "Thinking" expired at 15 s with no frame, and three mutations about reasoning and
    tool output could not be run.)
    """

    outcome, stage, reason = verdict
    if mutation.scenario and outcome == "fail" and stage == "state_shown" and "state_shown" not in mutation.stages:
        return "pass", None, "", reason
    return outcome, stage, reason, None


def _verdict(results: list[tt.CellResult]) -> tuple[str, str | None, str]:
    cell = results[0]
    return cell.outcome, cell.first_failed_stage, (cell.reason or "").strip()


def why_not(row: dict[str, Any]) -> str:
    """One line saying why a row is not ok: which of the two cells went wrong, where, and the harness's own reason."""

    if row.get("note"):
        return str(row["note"])
    if row.get("baseline", "pass") != "pass":
        said = f"the unmutated cell came out {row['baseline']} at {row.get('baseline_stage') or '-'}, so the mutation cannot be judged"
        reason = row.get("baseline_reason")
    else:
        wanted = "a pass" if row["expected_stage"] == "(pass)" else f"a failure at {row['expected_stage']}"
        said = f"expected {wanted}, saw {row.get('observed_outcome')} at {row.get('observed_stage') or '-'}"
        reason = row.get("reason")
    first_line = " ".join(str(reason or "no reason recorded").split())
    return f"{said}: {first_line[:600]}"


def render_rows(rows: Sequence[dict[str, Any]]) -> list[str]:
    """The mutation table, then a line for each row that is not ok (NO), each late state an unmutated cell showed
    (FINDING) and each unmutated cell that needed a second try (RETRIED)."""

    lines = ["| check | cell | baseline | expected | observed | stage | ok |", "|---|---|---|---|---|---|---|"]
    for row in rows:
        notes = [note for note, on in (("second try", row.get("baseline_retried")), ("state finding", row.get("state_finding"))) if on]
        baseline = f"{row.get('baseline', '-')}{' (' + ', '.join(notes) + ')' if notes else ''}"
        lines.append(
            f"| {row['check']} | {row['cell']} | {baseline} | {row['expected_stage']} | "
            f"{row.get('observed_outcome', row.get('observed'))} | {row.get('observed_stage') or '-'} | {'yes' if row['ok'] else 'NO'} |"
        )
    for row in rows:
        if not row["ok"]:
            lines.append(f"NO {row['check']}: {why_not(row)}")
    # Not misses: the gate caught the defect. They are what the unmutated cells showed on the way.
    for row in rows:
        if row.get("state_finding"):
            lines.append(f"FINDING {row['cell']}, unmutated: {' '.join(str(row['state_finding']).split())[:600]}")
        if row.get("baseline_retried") and row.get("baseline") == "pass":
            lines.append(
                f"RETRIED {row['check']}: the unmutated cell failed once at {' '.join(row['baseline_first_failure'].split())[:600]}"
            )
    return lines


def run_mutations(args: argparse.Namespace) -> int:
    pace = parse_pacing(
        {"rate": args.rate, "tokens": args.tokens, "marker_every": 20, "initial_delay": 1.0, "marker_prefix": "TT", "reasoning_tokens": 5}
    )
    expectations = tt.load_expectations()
    base_engine, base_cli = args.engine.resolve(), args.longhouse_cli.resolve()
    rows: list[dict[str, Any]] = []
    last_kinds: dict[str, Any] = {}  # what the most recent live-kinds cell saw, kept on a row that is not ok
    last_reply: dict[str, Any] = {}  # the same for the most recent reply-text cell

    def record(row: dict[str, Any]) -> None:
        """Keep the row, and the file up to date: a run that hits its time limit still leaves every row it finished."""

        seen = last_kinds if "(" in row["cell"] else last_reply
        if not row["ok"] and seen:
            row["evidence"] = dict(seen)
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
        if "sessions_after" in last_kinds:
            last_kinds["sessions_after"] = last_kinds["sessions_after"][:10]  # which session served what: a launch can leave two
        if result.get("sse_frames"):
            last_kinds["sse_frames"] = [
                {k: f.get(k) for k in ("t", "kind", "tool_name", "text_len", "text_tail")} for f in result["sse_frames"][:200]
            ]
        outcome, stage, reason = kinds_verdict(result)
        if outcome in {"measured", "not_measured"}:
            return "fail", "preflight", "the cell has no published-kind expectation"
        return outcome, stage, reason

    def cell(spec: tt.CellSpec, engine: Path, cli: Path, **extra: Any) -> tt.CellResult:
        result = tt.run_suite([spec], pace=extra.pop("pace", pace), engine=engine, longhouse_cli=cli, expectations=expectations, **extra)[0]
        last_reply.clear()
        last_reply.update({k: v for k in REPLY_EVIDENCE_KEYS if (v := getattr(result, k, None))})
        last_reply["stage_reasons"] = {name: stage.get("reason") for name, stage in result.stages.items() if not stage.get("ok", True)}
        return result

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
                outcome, stage, reason, finding = judge_unmutated(m, unmutated())
                return outcome, stage, reason, ({"reason": finding, "evidence": dict(last_kinds)} if finding else None)

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
                shadow = {"PYTHONPATH": str(prepare_server_copy(m, SCRATCH / f"server-{m.name}"))}
                if m.scenario:
                    mutated_kinds = kinds_cell(spec, base_engine, base_cli, host_environment=shadow, **m.run_options)
                else:
                    mutated = cell(spec, base_engine, base_cli, host_environment=shadow)
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
    print("\n".join(render_rows(rows)))
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
