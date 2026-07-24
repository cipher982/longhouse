# Durable Agent Operation Wake v1

Status: Iteration A active; operation wake deferred
Owner: machine surface / release tooling
Created: 2026-07-24

## Problem

An agent that starts a long mechanical operation should not repeatedly invoke a
model merely to learn that nothing changed. The model should run when judgment
is required: after success, failure, timeout, lost ownership, or an explicit
human intervention request.

The v0.1.30 release exposed both a repository correctness bug and a missing
agent-facing primitive. A synchronous release command waited for exact-SHA
evidence that never existed. The harness then resumed the model every 30 to 60
seconds to drain unchanged output. The result was dozens of model turns with no
new evidence or decision.

The readiness loop also repeated every check on every attempt. That included an
isolated PyPI installation and fresh engine/Desktop artifact downloads. A
nominal `--timeout 7200 --poll 30` could therefore attempt up to 240 complete
package and artifact validations. Individual attempts took much longer than 30
seconds, so the visible cadence was dominated by immutable work being repeated.
This was a material machine/network cost in addition to the model-turn cost.

This document separates those failures:

1. release readiness must distinguish missing evidence from work in progress;
2. agents need one provider-neutral way to register a mechanical wait and be
   resumed only at a meaningful transition.

## Evidence From v0.1.30

Release commit: `97a6670a16f15f950733c0ce11fadf481bf910af`

### Actual release operations

| Operation | Run | Observed wall time | Result |
| --- | --- | ---: | --- |
| Publish to PyPI | `30111753251` | 54s | success |
| Local Runtime Binary Release | `30111753067` | 10m 23s | success |
| Deploy and Verify | `30111752646` | 5m 33s | success |
| Launch Gate | `30111753209` | 9m 49s | success |
| Hosted Live QA | `30112093194` | 5m 21s | success |

The local runtime release was not stuck. Its longest useful work was native
compilation and signed macOS packaging:

| Release job or step | Observed time |
| --- | ---: |
| Linux x64 native binaries | 4m 59s |
| Linux arm64 native binaries | 4m 58s |
| macOS build, app, signing, notarization, and DMG | 9m 21s |
| macOS app notarization and staple | 1m 04s |
| macOS DMG notarization and staple | 1m 03s |
| Install and verify published release on macOS | 42s |
| Install and verify published release on Ubuntu | 36s |

The release produced notarized public assets and independently installed the
published wheel, fetched the matching engine through that wheel, and verified
version plus exact build commit on macOS and Ubuntu.

### The impossible wait

`scripts/ops/release.sh` then ran:

```text
launch-readiness.py --sha 97a6670... --wait --timeout 7200 --poll 30
```

The default readiness set required `Installer Validation Ring`. GitHub returned
no run for that workflow and commit. The monitor represented this as:

```text
Launch readiness pending for 97a6670a16f1:
workflow:Installer Validation Ring; retrying in 30s
```

It could repeat that message for two hours because a missing workflow was
nonterminal.

The workflow trigger explains why no run existed:

```yaml
on:
  push:
    tags-ignore:
      - 'v*'
    paths:
      - 'server/pyproject.toml'
      - 'engine/**'
```

Defining only `tags-ignore` restricts the push trigger to tag refs; it does not
enable branch pushes. Recent workflow history confirms the ring ran through
`schedule`, `pull_request`, and `workflow_dispatch`, but not `push`. The release
commit changed both `server/pyproject.toml` and `engine/Cargo.toml`, yet no push
run was created.

The most recent manually dispatched full ring, run `30053979230`, completed in
7m 02s. Its useful jobs were:

| Job | Observed time |
| --- | ---: |
| macOS packaging smoke | 1m 09s |
| Ubuntu hermetic installer | 4m 39s |
| macOS hermetic installer | 6m 57s |

The hours of perceived release latency therefore came from waiting for absent
evidence, not slow CI or Apple services.

## Goals

1. Never describe missing workflow evidence as running work.
2. Never require an LLM turn to observe an unchanged mechanical wait.
3. Resume the owning managed session once when a registered operation becomes
   terminal or requires judgment.
4. Use the same contract from Claude, Codex, OpenCode, and future managed
   providers.
5. Preserve raw command output, exit status, external run identifiers, and
   timing evidence.
6. Make interruption, machine restart, and lost local process ownership
   explicit.
7. Keep the first implementation small enough for one developer and a
   prelaunch product.

## Non-goals

- a user-facing jobs product;
- workflow DAGs, dependencies, priorities, schedules, or queues;
- automatic retries of mutating commands;
- model-authored callback code;
- remote shell orchestration;
- keeping provider processes alive while an operation runs;
- pretending a local child process survives machine restart;
- provider-specific completion integrations;
- progress summaries generated by an LLM;
- replacing external workflow, deployment, or provider-native run stores.

## First Principles

- Mechanical waiting is code, not model judgment.
- A durable operation identifier is not a PID.
- External systems remain authoritative for their own runs.
- Local process exit is evidence; command success is not task success unless
  the command's contract says so.
- Register before returning control to the provider.
- Emit events on state transitions, not polling intervals.
- Never replay a mutating command after an ambiguous crash.
- Store bounded metadata and log references, not unbounded output in SQLite.
- Resume a durable session; do not depend on an idle provider process.
- MCP and provider configuration are adapters. The machine API and Machine
  Agent own the behavior.

## Approved Deliverable: Repository Correctness

These changes are independent of the product primitive and should ship first.

### Correct the installer trigger

Use an explicit branch filter for path-filtered push validation:

```yaml
on:
  push:
    branches:
      - '**'
    paths:
      - ...
```

A branch-only filter naturally excludes release tags. Do not combine branch
and tag assumptions in one implicit filter.

### Represent workflow states honestly

Readiness workflow checks must return one of:

```text
missing | pending | succeeded | failed
```

`missing` means no exact-SHA run exists. Under `--wait`, it gets a short
discovery grace to cover GitHub event creation latency. After the grace it is a
terminal failure with the exact manual dispatch command. It is never allowed to
consume the full operation timeout.

`pending` means a concrete run ID exists with a nonterminal GitHub status.

Human output prints only state transitions. A `--json` status call always
returns the complete current snapshot.

Live-surface propagation remains bounded by the overall readiness timeout. A
temporarily unreachable or stale live URL is not made terminal by the workflow
discovery grace because real deploy propagation may take longer than workflow
event creation.

### Do not repeat immutable expensive checks

Within one wait invocation, successful immutable checks are cached:

- the PyPI package identity for `(tag, SHA)`;
- released runtime artifact identity for `(tag, SHA, component)`.

Workflow state and live surfaces remain cheap and dynamic, so they are observed
on every attempt. Latest-release identity is also observed on every attempt;
the package/artifact cache key changes if its tag changes. Failed checks are not
cached.

### Use release-appropriate evidence

The source-oriented Installer Validation Ring and the release artifact verifier
answer different questions:

- Installer Validation Ring: can source plus installer scripts produce a
  working hermetic install?
- Local Runtime Binary Release verification: can a clean machine install the
  exact published wheel, fetch the exact published engine, and execute it?

The release workflow already runs the second check on macOS and Ubuntu. Default
launch readiness therefore requires exact-SHA `Local Runtime Binary Release`
evidence and released artifact identity, not the path-filtered source ring. The
source ring remains required on relevant PR/main changes and on its
nightly/manual lanes.

This also avoids rebuilding the engine for two installer jobs plus a separate
macOS packaging job after the release workflow already built three release
architectures and verified the released artifacts.

## Deferred Direction: Durable Operation Wake

Architecture review approved the repository correctness slice and rejected a
new operation subsystem. The repo already has a Runtime Host-owned
`MachineControlOperation` lifecycle with durable owner/device/session binding,
queued/running/terminal states, timeouts, command-result reconciliation, and
idempotent terminal guards. A second Machine-Agent-local SQLite operation store
would duplicate authority and reconciliation.

The remainder of this section records the product direction, not work approved
for this iteration. A later proposal must extend the existing operation
lifecycle with a narrowly scoped shell operation and add an explicit
system-origin session input. The current directed-input envelope cannot be
reused: it requires a peer source session and labels its body untrusted peer
input.

### Agent interface

The possible agent-facing primitive is a native CLI command:

```text
longhouse operation start \
  --name release-v0.1.31 \
  --timeout 2h \
  -- make release VERSION=v0.1.31
```

It returns immediately after durable registration:

```json
{
  "operation_id": "op_...",
  "session_id": "...",
  "state": "running",
  "log_path": "...",
  "started_at": "...",
  "timeout_at": "..."
}
```

Possible supporting commands:

```text
longhouse operation status <operation-id> --json
longhouse operation list --current-session --json
longhouse operation cancel <operation-id>
```

Cancellation is explicit and separate from timeout. A timed-out command is not
assumed safe to kill.

### Durable record

Do not create this previously proposed Machine-Agent-local record. Extend the
existing Runtime Host `MachineControlOperation` / live-store operation record
only if a later implementation is approved. The fields that still matter for a
future local shell subtype are:

```text
operation_id
session_id
name
cwd
argv_json
state                 registered|running|succeeded|failed|timed_out|lost|cancelled
created_at
started_at
finished_at
timeout_at
supervisor_pid
child_pid
child_start_identity
exit_code
log_path
last_error
terminal_event_sent_at
```

Secrets are not stored in argv or the record. The launched process inherits the
caller's environment in memory; the record stores only an allowlisted redacted
environment summary if diagnostics need it.

### Supervision

The native CLI asks the already-running Machine Agent to register and launch
the command. The Machine Agent redirects stdout and stderr to one bounded local
log artifact and owns process-group identity. The provider tool call returns
after the child is started and the durable record says `running`.

If the Machine Agent restarts:

- it validates PID plus process start identity;
- a matching live child is reacquired for observation where the platform
  permits it;
- a terminal child with recoverable status is finalized;
- ambiguous or vanished ownership becomes `lost`;
- a mutating command is never automatically rerun.

V1 may mark local operations `lost` after Machine Agent restart if portable
exit-status recovery is not available. Honest loss is preferable to a second
supervisor protocol.

### External operations

No external-operation adapter is approved. `gh run watch <id> --exit-status`
already provides a correct mechanical child process for GitHub Actions. A
future supervised-shell operation can run it without teaching Runtime Host the
GitHub API.

### Terminal wake

On the first terminal transition, the Machine Agent submits one attributed
session input to the owning session through the existing canonical managed
input path. The event contains bounded facts and a log reference:

```text
Longhouse operation completed.
operation_id: op_...
name: release-v0.1.31
state: failed
exit_code: 1
duration_seconds: 643
log_path: ...
last_output: <bounded tail>

Inspect the evidence and continue the original task.
```

This is not a peer message and must not pretend another model sent it. It is a
Longhouse system-originated input associated with the owning session. Runtime
Host persists it before live delivery. If no provider invocation is currently
available, the event remains durable and appears on the next resume. If the
provider is busy, it follows the ordinary safe input boundary rather than
active-turn steer.

`terminal_event_sent_at` makes emission idempotent. Reconciliation may retry
delivery without creating a second durable completion event.

### Provider behavior

Providers do not implement operation watching. Claude, Codex, and OpenCode use
the same native command or thin MCP wrapper. The current managed session is
derived from authenticated launch context, never a model-supplied session ID.

Shadow sessions cannot register a wake because Longhouse does not own a control
path. The command fails explicitly instead of silently starting an unmanaged
background process.

## Agent Guidance

Repository instructions should establish one rule:

> If a command may outlive the tool's foreground execution window and no work
> remains useful while it runs, register it as a Longhouse operation and end the
> turn. Do not repeatedly ask the model to poll unchanged state. Resume only on
> a terminal operation event or a human message.

Agents may deliberately inspect status when new judgment is useful. They should
not generate periodic reassurance messages.

## Failure Semantics

| Condition | State | Wake? | Automatic action |
| --- | --- | --- | --- |
| command exits 0 | `succeeded` | once | none |
| command exits nonzero | `failed` | once | none |
| declared timeout reached | `timed_out` | once | notify or explicit terminate |
| process identity disappears ambiguously | `lost` | once | never replay |
| user cancels | `cancelled` | once | terminate owned process group |
| external run succeeds | `succeeded` | once | none |
| external run fails/cancels | `failed` | once | none |
| Runtime Host unavailable | terminal state retained | later retry | bounded transport retry |
| provider unavailable | input retained | next safe resume | no provider auto-start |

## Security

- Operation registration requires current managed-session authority.
- `cwd` must exist and be accessible to the local user.
- Commands execute with the local user's existing authority; Longhouse does not
  elevate privileges.
- Never store the full inherited environment.
- Logs use user-only permissions and have a size cap plus rotation.
- Cancellation validates recorded PID and process start identity before
  signaling the process group.
- System completion input is server-authenticated and cannot be forged by a
  model-controlled target session ID.
- Operation metadata sent to Runtime Host excludes argv and local environment
  by default.

## Observability

Each operation records timestamps for registration, process start, terminal
observation, durable event persistence, and provider delivery when available.
The useful latency split is:

```text
command wall time
terminal observation latency
terminal event persistence latency
provider delivery latency
agent resume latency
```

The UI and CLI may show current state without waking a model. V1 does not build a
new operations page.

## Options Considered

### Keep blocking commands and ask each harness to wait better

Rejected as the system solution. It depends on provider/harness behavior and
does not survive an agent turn, context compaction, or a disconnected client.
It remains an acceptable foreground path for short operations.

### Have repository scripts send provider-specific messages

Rejected. Release scripts should not know whether Claude, Codex, or OpenCode
owns the session, and should not possess provider credentials.

### Poll from Runtime Host

Rejected for local commands. The Runtime Host may be remote and does not own the
local process. The Machine Agent is the execution owner.

### Build a general jobs platform

Rejected. V1 needs supervision, evidence, and one wake event. Scheduling,
dependencies, retries, and job UX do not improve the launch product.

### Only fix release scripts

Insufficient. It prevents this exact false wait but leaves tests, delegated runs,
deploys, and future external operations vulnerable to the same LLM polling
pattern.

## Approved Implementation Sequence

### Iteration A: repository correctness — implement now

1. Fix the Installer Validation Ring branch trigger.
2. Add explicit missing/pending/succeeded/failed workflow states.
3. Add a short missing-run discovery grace under `--wait`.
4. Print only transitions while waiting.
5. Make release readiness depend on release-appropriate exact-SHA evidence and
   stop requiring the source installer ring as an implicit release gate.
6. Add focused tests for missing-run termination, late run discovery, pending
   run wait, terminal failure, and transition-only output.

Existing coverage in `server/tests_lite/test_launch_readiness.py` already proves
exact identity checks, latest run selection, dispatch hints, and immediate
terminal workflow failures. Extend that file; do not create a parallel test
harness.

### Iteration B: local supervised operation — deferred

1. Extend `MachineControlOperation`; do not add another store.
2. Define a `shell` command kind and exact local execution ownership.
3. Define a new system-origin input envelope; do not fabricate peer identity.
4. Launch one supervised command with bounded logs.
5. Reconcile terminal, timeout, cancellation, and explicit `lost` states.
6. Persist and deliver one idempotent completion input.
7. Add a thin coordination MCP binding only after the native/API path works.

### Iteration C: external adapters — cut

Use `gh run watch` as the supervised child. Reconsider an adapter only after a
second concrete system cannot be represented safely by a supervised command.

## Verification

Repository correctness:

- fixture tests for all four workflow states;
- a branch push touching a path-filtered installer file creates an exact-SHA
  Installer Validation Ring run;
- a deliberately missing required workflow fails after the discovery grace,
  not the global timeout;
- release readiness passes from release-event evidence without rebuilding the
  source installer ring.

Operation primitive:

- a short success command produces one completion input;
- a failing command preserves exit code and log tail;
- a busy provider receives completion at the next safe boundary;
- a cold managed session retains completion for resume without auto-start;
- restart produces either reacquired observation or explicit `lost`, never a
  duplicate command;
- repeated reconciliation emits one durable terminal event;
- cancellation cannot signal an unrelated reused PID;
- Claude, Codex, and OpenCode consume the same completion contract;
- a live disposable GitHub Actions run wakes one managed session exactly once.

## Decision

Accepted:

- Iteration A is the only approved implementation in this change.
- Missing workflow evidence gets a five-minute discovery grace, then fails.
- Successful package and runtime-artifact checks are memoized by immutable
  identity.
- Default exact-SHA evidence replaces Installer Validation Ring with Local
  Runtime Binary Release.
- Release readiness timeout drops from two hours to 30 minutes.
- Human wait output emits only state transitions.
- A future operation wake must extend existing `MachineControlOperation` and
  use a new system-origin input envelope.

Existing coverage in `server/tests_lite/test_launch_readiness.py` proves exact
identity checks, latest run selection, dispatch hints, and immediate terminal
workflow failures. The implementation adds the previously missing wait-loop,
grace-clock, transition-output, and memoization coverage.

Rejected:

- A five-minute terminal deadline for unreachable live surfaces. Live deploy
  propagation is dynamic and remains governed by the overall bounded timeout.

Answered future questions:

1. Iteration A ships alone.
2. No new local operation store; extend the existing operation lifecycle.
3. No suitable system-origin input exists; a new envelope type is required.
4. A first local-shell version reports explicit `lost`; it does not replay or
   invent portable child reacquisition.
5. No GitHub adapter; supervise `gh run watch` if the later shell primitive is
   approved.
