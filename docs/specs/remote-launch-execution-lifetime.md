# Remote Launch Execution Lifetime

Status: Draft
Owner: machine control + mobile/web launch UX
Updated: 2026-06-16

## Summary

Longhouse remote launch currently creates live managed sessions from iOS and
web. For Codex, that means a detached-UI app-server and Longhouse bridge: no
visible terminal TUI, but a real provider process remains running and steerable.
That is correct for an explicitly live remote-control session, but it is a bad
default for "start work from my phone."

The product correction should stay small:

- make **one-shot** execution the default for iOS/web task launch;
- keep **live control** available as an explicit mode;
- model this on top of the existing session kernel instead of adding a parallel
  lifecycle system;
- do not create a jobs platform before launch.

Use a small launch input enum:

```text
execution_lifetime = one_shot | live_control
```

This is intentionally not a four-shape taxonomy. It is the product distinction
the user needs: "run this turn and exit" vs "keep a controllable provider
process alive."

## Why This Exists

The menu bar investigation exposed many background Codex rows. Those rows were
not stale database ghosts. They were mostly real managed Codex sessions launched
without a foreground TUI.

The confusing part is product semantics:

- On a laptop, a user expects closing the terminal window to end the visible
  managed process.
- On iOS or web, backing out of a session view does not close anything, because
  there is no local terminal window to own that lifecycle.
- Remote launch v1 intentionally starts a live managed session. For Codex, the
  current spec says remote launch leaves a long-running bridge and app-server
  session rather than one-shot prompt-and-exit execution.

That makes the default path surprising: the user meant "do this task," but
Longhouse leased an invisible process.

## Existing Kernel

Do not invent a second process-lifetime model. Longhouse already has the right
durable nouns:

- `SessionRun`: one provider CLI process invocation lifetime. It records host,
  pid, cwd, argv, start/end, and exit status.
- `SessionConnection`: Longhouse's relationship to a run. A bridge can be
  attached, degraded, detached, released, or ended without ending the session.
- `SessionLaunchAttempt`: pre-process remote/managed launch lifecycle. It can
  exist before a run is created and later points at a `run_id`.
- `AgentSession`: the durable user-facing session/conversation. A provider
  process can exit while the Longhouse session remains follow-up-able.

Therefore:

- `execution_lifetime` is a launch request/attempt input, not a new durable
  source of process truth.
- One-shot completion ends the `SessionRun` and its `SessionConnection`.
- One-shot completion does **not** automatically end `AgentSession`.
- Follow-up one-shot work should create a new `SessionRun` under the same
  Longhouse session/thread when provider resume semantics prove safe.

## Naming Collision To Fix

The current remote-launch lifecycle projection uses `launch_state="live"` when
a `SessionLaunchAttempt` has a `run_id` or is adopted. That string means
"launch succeeded and has a run," not "the provider process should remain alive
after idle."

This collides with this spec's live-control meaning.

Before implementation, either:

- rename the public launch projection value from `live` to something like
  `launched`, `ready`, or `active`; or
- keep `launch_state="live"` for compatibility but introduce an orthogonal
  `execution_lifetime` field everywhere UI/product copy talks about one-shot vs
  live control.

Recommendation: preserve `launch_state="live"` short-term for API
compatibility, but never use it in copy or logic as the execution-lifetime
axis. Add explicit tests proving `launch_state` and `execution_lifetime` are
orthogonal.

## Provider Research

Primary docs and local CLI help show that the providers already expose
prompt-and-exit style execution paths:

| Provider | One-shot surface | Notes |
| --- | --- | --- |
| Codex | `codex exec` / `codex e` | OpenAI documents `codex exec` as stable, non-interactive, suitable for scripted or CI-style runs, with stdout/JSONL output and resume support. Local `codex exec --help` confirms "Run Codex non-interactively." Source: https://developers.openai.com/codex/cli/reference |
| Claude Code | `claude -p` / `--print` | Anthropic documents `claude -p "query"` as "Query via SDK, then exit." The same reference separately documents background agents, daemon stop/status, and remote-control, which supports keeping one-shot and live-control concepts separate. Source: https://code.claude.com/docs/en/cli-reference |
| Antigravity CLI | `agy` managed launch | Antigravity replaces the deprecated Gemini CLI provider surface. |
| OpenCode | `opencode run` | OpenCode documents `opencode run [message..]` as non-interactive execution without launching the full TUI. It separately documents `opencode serve` for a headless HTTP server. Source: https://opencode.ai/docs/cli/ |
| Antigravity CLI | `agy -p` / `--print` | Google docs snippets describe `-p` as a one-shot prompt flag. Local `agy --help` confirms `--print`: "Run a single prompt non-interactively and print the response." Longhouse must still not advertise Antigravity remote launch until a canary proves transcript, exit, and hook semantics. Source: https://antigravity.google/docs/cli-best-practices |

Longhouse's managed-provider guidance already distinguishes detached-UI managed
sessions from one-shot/batch execution. This spec applies that distinction to
iOS/web defaults.

## Product Principles

1. **Process lifetime is not session lifetime.**
   The transcript/session record can persist after the provider process exits.

2. **Remote task launch should default to one-shot.**
   A phone/web user asking an agent to do work is usually asking for a bounded
   run, not an invisible process lease.

3. **Live control must be explicit.**
   If Longhouse leaves a provider process running after a turn, the UI should say
   so before launch and show stop controls afterward.

4. **No jobs platform by accident.**
   Long-running tests, deploy monitors, and foreground watchers are active-run
   behavior. A dev server intentionally left running should be represented as an
   artifact/child process, not a reason to keep the provider harness idle
   forever.

5. **Keep raw dimensions separate.**
   `execution_lifetime` answers "should the provider process survive idle?"
   `SessionRun.ended_at` answers "did this provider invocation exit?"
   `SessionConnection.state` answers "does Longhouse still have control?"
   Runtime phase answers "is a turn active/blocked/idle?"

6. **Prefer one obvious seam over generic abstraction.**
   Add one-shot beside live control. Avoid a broad provider-runner framework
   until at least two providers prove the same implementation shape.

## UX Shape

### Default iOS/Web Launch

The default launch flow should ask for an initial prompt and run once:

```text
Start Agent
Machine      cinder
Workspace    /Users/davidrose/git/zerg/longhouse
Provider     Codex
Mode         Run once
Prompt       Fix the failing telemetry test and summarize the change.
```

After launch:

- session detail opens and streams output;
- completed one-shot runs show as complete/idle, not as running background
  sessions;
- follow-up sends another one-shot invocation, using provider resume only after
  the Phase 0 canary proves it binds cleanly to the same Longhouse thread;
- closing/backing out of the view does not need to kill a process because there
  should be no idle provider process to kill.

### Explicit Live-Control Launch

Live-control launch remains available, but it is named and visible:

```text
Mode         Keep live session open
```

Live-control sessions should show:

- foreground/background/detached UI presence where available;
- last active turn;
- stop control;
- idle age;
- optional "stop when idle" policy.

An empty session launch only makes sense in live-control mode. One-shot launch
requires an initial prompt. This intentionally reverses remote-launch v1's
"create an empty steerable session" default.

## API And Machine Contract

Keep the user-facing API narrow, but preserve a clean machine seam.

Recommended shape:

- `POST /api/sessions/launch` accepts:
  - `execution_lifetime`;
  - `initial_prompt`.
- Runtime Host validates:
  - omitted `execution_lifetime` preserves legacy behavior: `live_control`;
  - new web/iOS clients must send `execution_lifetime` explicitly;
  - `one_shot` requires `initial_prompt`;
  - `live_control` may omit `initial_prompt`;
  - target machine advertises the matching provider operation.
- Runtime Host dispatches different typed machine commands:
  - `session.run_once` for one-shot;
  - existing `session.launch` for live-control managed launch.

Do not infer one-shot from client type. The client sends the field. Legacy
clients that omit it keep current live-control semantics until they update.

## Provider Support Gating

Each provider operation must be advertised independently.

The managed-provider contract manifest currently has operation flags such as
`launch_remote`, `send_input`, `terminate`, and matching `operation_evidence`.
Adding one-shot should extend that same manifest rather than creating a second
support source.

New operations:

```text
run_once
resume_run_once
```

Possible `machine_control_supports` bits:

```text
codex.run_once
codex.resume_run_once
claude.run_once
opencode.run_once
```

Rules:

- `*.run_once` is not implied by `*.launch`.
- `*.launch` is not implied by `*.run_once`.
- UI must handle a provider that supports one but not the other.
- No support bit is emitted until provider canaries prove command shape,
  transcript binding, exit status, and permission behavior.
- Antigravity remains send-only/live-proof in Longhouse until its own canary
  proves more.

## Permission Posture

This is the hardest product/security edge.

One-shot must not hang forever behind an invisible permission prompt. But
auto-approving everything from an iOS/web-launched cwd would widen the original
remote-launch trust model. The first one-shot implementation must choose an
explicit provider-specific permission posture.

Default policy:

- Prefer provider flags/config that fail fast on unapproved actions rather than
  waiting for TTY input.
- Surface the failure as a visible blocked/permission result with logs.
- Do not default remote one-shot to provider "yolo" or full auto-approval.
- If a provider exposes structured permission prompts in non-interactive mode,
  Longhouse may surface those as active blockers later.
- Revisit auto-approval only behind an explicit user/machine policy, after cwd
  allowlist and risk copy are clear.

This keeps the one-shot default safe enough to dogfood without silently
escalating the original launch surface.

## Runtime And Archive Semantics

One-shot execution still needs a live lane and a durable lane.

Live lane:

- stream provider stdout/JSON events to session detail quickly;
- map provider phase to Runtime Host runtime events where possible;
- surface permission blocks or input requests as active blockers;
- record process start/end on `SessionRun`.

Durable lane:

- ingest the provider's native transcript when available;
- attach stdout/stderr/log artifacts for providers whose one-shot mode does not
  write a normal transcript;
- record clean exit vs crash/permission/timeout in `SessionRun.exit_status`;
- end/release the `SessionConnection`;
- leave `AgentSession` open for follow-up unless the user explicitly ends it.

Local health should primarily benefit from process absence: a cleanly exited
one-shot provider process should simply disappear from process scans. The
important missing signal is not another UI bucket; it is clean `SessionRun`
completion so Longhouse can distinguish success from orphan/crash.

## Provider-Specific Integration Notes

### Codex

`codex exec --json -C <cwd> <prompt>` is a sibling path to the existing
app-server bridge, not a flag on detached-UI launch.

Phase 0 must prove:

- JSONL event stream can drive live detail;
- transcript/rollout file binding exists and maps to the Longhouse thread;
- `codex exec resume` creates coherent follow-up runs under one thread;
- permission prompts fail/surface instead of hanging;
- no `codex-bridge` state or app-server process remains after clean exit.

### Claude

`claude -p --output-format stream-json` bypasses Longhouse's current channel
control path. That means no live channel send, interrupt, or active-turn steer
for one-shot Claude unless a separate structured control path is proven.

Phase 0 must prove transcript path, stream-json event shape, exit status, and
permission behavior before `claude.run_once` is advertised.

### OpenCode

`opencode run --format json --dir <cwd>` bypasses the existing `opencode serve`
plus attach path. It should be treated as a new sibling integration with its own
transcript and permission proof.

### Gemini

Gemini remains legacy/import unless Longhouse intentionally re-enables launch.
Antigravity does not expose a proven run-once lane yet.

### Antigravity

Antigravity has local `agy -p`, but Longhouse currently treats Antigravity as a
managed local wrapper plus hook-inbox/send-only surface. Do not advertise
remote launch, interrupt, steer, or run-once until a provider canary proves the
semantics.

## Long-Running Edge Cases

One-shot does not mean "the process must exit quickly." It means "the provider
process should not survive a completed turn."

Examples:

- If the agent runs tests for 20 minutes, the one-shot process remains active
  until the run completes.
- If the agent starts a dev server in the background and then completes, the
  provider process can exit; the dev server should be represented as an artifact
  or detected child/background process.
- If the user wants an agent to monitor, wait, or accept later steering, they
  should choose live-control mode.

Reaper safety: existing bridge reapers must never classify an active one-shot
run as an orphan solely because it has no TUI. One-shot Codex should not create
bridge state at all; if any provider does create helper state, reaper tests must
cover active long one-shot runs.

## Migration And Cleanup

Existing detached-UI sessions are real processes. Do not silently kill them on
upgrade.

Add user-facing cleanup:

- stop one background live-control session;
- stop all idle background live-control sessions;
- stop all detached/background Codex app-server bridges;
- use local-health JSON as the canonical local cleanup/status contract so menu
  bar, web, and iOS use the same names.

After one-shot default ships, old live background rows should become rarer and
more intentional.

## Implementation Plan

### Phase 0 - Proof Matrix And Spec Lock

- Record provider command matrix with exact versions and local help output.
- Add canary scripts for:
  - `codex exec`;
  - `codex exec resume`;
  - `claude -p --output-format stream-json`;
  - `opencode run --format json`;
  - `agy -p` only as non-advertised evidence.
- For each canary, record:
  - transcript file/path behavior;
  - provider session/resume id behavior;
  - exit status;
  - permission prompt behavior;
  - whether native provider ingest produces duplicate or missing Longhouse
    events.
- Decide the exact `launch_state` naming compatibility plan.

### Phase 1 - Kernel And Contract

- Add `execution_lifetime` to launch request, launch attempt metadata, and
  session projection as an orthogonal field.
- Preserve omitted-field API compatibility: omitted means `live_control`.
- Extend managed-provider contract manifest with `run_once` operation evidence.
- Add `session.run_once` machine command.
- Add backend tests for validation, support gating, and compatibility.

### Phase 2 - Codex One-Shot

- Implement Codex one-shot using `codex exec --json -C <cwd> <prompt>`.
- Create a `SessionRun` at process start.
- Stream JSONL into live session detail.
- Persist final status, exit code, stdout/stderr artifact paths, and provider
  session/resume id when proven.
- End the `SessionRun` and `SessionConnection` on provider exit.
- Verify no bridge/app-server state remains after clean one-shot exit.

### Phase 3 - UI Default Flip

- Update web/iOS launch flow:
  - prompt required by default;
  - default mode is "Run once";
  - clients send `execution_lifetime=one_shot` explicitly;
  - "Keep live session open" is explicit.
- Add stop/cleanup actions for existing live-control background sessions.
- Smoke test from web and iOS dogfood paths.

### Phase 4 - Provider Expansion

- Claude: implement only after Phase 0 proves stream-json, transcript, and
  permission behavior.
- OpenCode: implement only after `opencode run` proof.
- Gemini: defer unless launch is intentionally reintroduced.
- Antigravity: defer remote launch/run-once until canary evidence is strong
  enough to change the managed-provider contract.

### Phase 5 - Live-Control Cleanup Policy

- Add optional idle-stop policy for explicit live-control remote sessions.
- Consider a one-time dogfood cleanup prompt for old detached-UI sessions.
- Document in managed-provider guidance that `detached_ui` is live-control, not
  one-shot.

## Test Plan

- Server tests:
  - omitted `execution_lifetime` preserves live-control behavior;
  - web/iOS payloads send `execution_lifetime` explicitly;
  - `one_shot` requires prompt;
  - unsupported `*.run_once` fails clearly;
  - `launch_state` and `execution_lifetime` are orthogonal.
- Kernel tests:
  - one-shot completion ends `SessionRun` and `SessionConnection`, not
    `AgentSession`;
  - follow-up can create a new run under the same session/thread;
  - clean exit and crash/permission/timeout statuses are distinguishable.
- Engine tests:
  - `session.run_once` dispatch validates cwd locally;
  - provider argv is exact and token-safe;
  - process exit emits ended lifecycle;
  - no bridge/app-server state is left behind for one-shot Codex.
- Local health tests:
  - completed one-shot runs are not counted as background processes;
  - live detached-UI sessions still appear as background/live-control;
  - active long one-shot runs are not reaped as orphaned.
- Integration tests:
  - mocked provider JSONL/stdout streams;
  - provider permission-block behavior;
  - follow-up/resume id handling where supported.
- Dogfood smoke:
  - launch Codex one-shot from web;
  - see output stream;
  - process exits;
  - `SessionRun.exit_status` records clean completion;
  - menu bar count does not increase after completion.

## Open Questions

- Should the public projection rename `launch_state="live"` now or only add
  `execution_lifetime` alongside it? Recommendation: add the orthogonal field
  first, then rename only if the compatibility cost is small.
- Can `codex exec resume` map cleanly to Longhouse's existing thread and
  transcript aliases? This must be proven before promising follow-up context.
- How much provider stdout belongs in timeline content versus artifacts?
  Recommendation: live stream status, but treat provider-native transcript as
  archive truth when available.
- Can any provider offer structured permission prompts in one-shot mode?
  Recommendation: do not block Codex one-shot on this, but default to fail-fast
  instead of auto-approve.

## Hatch Opus Review Incorporated

This revision incorporates Hatch Opus pushback from 2026-06-16:

- grounded execution lifetime in `SessionRun` and `SessionConnection`;
- called out the existing `launch_state="live"` naming collision;
- changed one-shot completion from ending the session to ending the run and
  connection;
- added explicit permission posture;
- split provider parity into provider-specific proof requirements;
- made omitted-field compatibility explicit;
- promoted resume behavior to Phase 0 proof instead of assuming it.
