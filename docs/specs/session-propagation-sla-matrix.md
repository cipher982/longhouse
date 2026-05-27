# Session Propagation SLA Matrix

Status: Building
Last updated: 2026-05-11
Related: `managed-session-propagation-profiler.md`, `session-lifecycle-liveness-contract.md`, `session-signal-tier-model.md`
Manifest: `config/session-propagation-sla.toml`

## Purpose

This is the short operating contract for profiling whether Longhouse reflects
real provider sessions truthfully and quickly.

The longer profiler spec explains the philosophy and experiment design. This
document defines the current matrix we can measure, promote, and eventually run
in CI.

## Product SLA Shape

Warm realtime means the user is already on `/timeline`, initial data loaded,
and the timeline stream connected before the session change happens.

Cold timeline means page load from scratch. It is useful for regression testing
the product surface, but it is not the realtime propagation SLA.

Durable archive means canonical hosted transcript/history/search correctness.
It can trail the live lane by a small bounded window. Live UI must not wait for
archive ingest before showing managed-session truth.

Honest degradation means failure modes are labeled correctly. SSH loss,
terminal detach, process death, and machine offline are different facts.

## Manifest Vocabulary

The manifest intentionally freezes the strings that future CI and dogfood
aggregation will group by.

Statuses:

- `required`: implemented path that can become a gate
- `experimental`: measured and reported, not yet a hard gate
- `undefined`: intentionally not promised

Topologies:

- `hosted_runtime_host`: Machine Agent on a user machine, Runtime Host in hosted Longhouse
- `self_hosted_runtime_host`: Machine Agent on a user machine, Runtime Host on the user's always-on box
- `local_runtime_host`: Machine Agent and Runtime Host on the same local machine

Layers:

- `provider_process`
- `provider_transcript`
- `machine_agent`
- `hosted_db`
- `hosted_api`
- `timeline_sse`
- `browser_card`

Observers:

- `managed_sessions_snapshot`
- `unmanaged_session_bindings`
- `process_scan_snapshot`
- `machine_heartbeat`
- `provider_transcript`
- `claude_channel_state`
- `timeline_api`
- `timeline_sse`
- `browser_card`
- `hosted_db`
- `pty_and_codex_bridge`

Metric `legacy_aliases` entries exist only to bridge old artifact keys while
the current profiler is being migrated. New metrics and reports should use the
canonical metric IDs, not legacy aliases.

`ci_mode` tells automation what to do with a case:

- `gate`: run in CI and fail the job on product/SLA failure
- `report`: runnable in CI as a non-gating experimental profile
- `blocked`: intentionally specified, but not executable until its driver exists

Blocked cases must name the missing driver work in `blocked_reason`. That keeps
Claude/OpenCode coverage visible without pretending Codex's bridge mechanics
apply to them.

## Current Required Path

`managed_codex_warm_live_graceful_close`

This is the primary warm realtime path currently promoted to `required`.

It proves the warm managed Codex happy path:

- managed session card appears with correct ownership and capability truth
- live output reaches timeline SSE and browser card on the live lane
- graceful bridge shutdown closes the card without waiting for a slow backstop
- durable archive is observed but scored separately

`managed_codex_durable_archive`

This is the experimental durability companion for the same managed Codex happy
path. It proves canonical hosted transcript/archive catch-up without
deciding whether the already-open timeline felt realtime. Promote it only
after archive-path variance and provider preconditions are stable in batches.

`managed_codex_cold_timeline_closed`

This is the first cold timeline path. It creates and closes a managed Codex
session, waits for hosted truth, then opens a fresh browser timeline page. It
measures page navigation to target card paint and page navigation to closed
card paint. It is intentionally separate from the warm realtime SLA: cold load
answers "does a newly opened timeline show current truth quickly?" rather than
"did an already-open page update immediately?"

This is a cold browser/SPA profile against a warm Runtime Host: the managed
session was just created and closed, so server-side SQLite and reducer state may
still be hot. That is intentional for the user-perceived fresh-page SLA. The
profiler also reports `cold_timeline_card_to_close_paint_ms` as debug evidence.
For an already-closed session, `0ms` is expected because the card and closed
state should paint from the same initial DOM state. A non-zero value means the
page briefly painted stale/non-closed truth before correcting itself.

Current target budgets are in `config/session-propagation-sla.toml`. The
important user-facing targets are:

- cold timeline fresh navigation to target card paint: 2000ms target, 4000ms alarm
- cold timeline fresh navigation to closed card paint: 2000ms target, 4000ms alarm
- warm live output local truth to browser paint: 500ms target, 1000ms alarm
- warm graceful close local truth to browser paint: 1000ms target, 2000ms alarm
- warm close local truth to Runtime Host DB state: 500ms target, 1000ms alarm
- durable archive local transcript to hosted events: 3000ms target, 30000ms alarm

The profiler still records close-SSE watcher timings, but those are diagnostic.
The product SLA for close is the already-open browser card paint plus Runtime
Host state, because the extra watcher can lag behind the page's own timeline
stream and does not represent what the user saw.

## Experimental Paths

These should be measured and reported, but not treated as hard gates yet.

Managed Claude warm live and graceful close:
Claude uses native channel/MCP/stdin mechanics, not the Codex bridge. The
profiler must record channel state and process identity when it classifies a
Claude session. This case is still experimental, but it now has two POC tools:

- `make managed-claude-truth-probe ARGS="--session-id <id>"` captures
  observation-only truth surfaces for an existing managed Claude session.
- `make managed-claude-poc` launches a managed Claude PTY, sends one channel
  message, waits for a real assistant transcript response, exits, and captures
  before/after truth probes.

The current POC proves managed-Claude channel send and local transcript response
can work. The first run exposed the close gap: a smooth `/exit` could remove
the provider process locally while hosted runtime stayed `idle`. The current
implementation now has two live close lanes: `claude_channel_wrapper` posts
graceful `session_ended/provider_exit` when the wrapper observes provider exit,
and the Machine Agent's `claude_channel_scan` posts `process_gone` when a
previously observed managed Claude channel loses its provider PID or state file.
This path is still experimental until repeated profiler runs prove the browser
warm lane and durable archive lane independently.

Managed OpenCode lifecycle:
Managed OpenCode now has a first-class server-bridge control path for launch,
reattach, send, and interrupt. It remains blocked only for active-turn steer
until the server/TUI prompt APIs prove bounded mid-turn injection and idle
rejection.

Unmanaged Codex and Claude graceful close:
These compatibility/import paths should be truthful, but they do not get the
same terminal-class realtime promise as managed control paths. Unmanaged Codex
is runnable as a report-only CI case; unmanaged Claude remains blocked until a
safe direct-run driver exists.

Managed Codex Ctrl-C, SSH disconnect, and provider kill:
These require a real PTY or SSH harness. The purpose is to distinguish terminal
relationship loss from provider process death.

## Undefined Paths

Unmanaged live send is not promised. The UI should not imply live control for
unmanaged sessions.

OpenCode remote send/interrupt is promised only for `opencode_server_bridge`
sessions on machines whose control channel advertises `opencode.*` support.
OpenCode active-turn steer is not promised.

## Promotion Rules

A scenario starts as `experimental`.

Promote it to `required` only after:

- the profiler has a deterministic scenario implementation
- unit tests cover the artifact/report parsing
- the run captures the relevant observer layers
- at least one clean manual run proves the path exists
- repeated dogfood runs are stable enough to reason about p50/p95

Demote a scenario if the product contract changes. Do not keep a failing gate
for a path Longhouse no longer promises.

## CI Ladder

Fast PR/local gate:
Validate the manifest, report parsing, reducer correctness, and fixture-backed
browser semantics.

Manual/pre-push profiler:
Run the required managed Codex warm-live path against the dogfood tenant and
save artifacts. Use workflow dispatch with `include_experimental=true` to also
run the report-only Codex cold timeline, durable archive, and unmanaged
baseline cases.

Nightly dogfood:
Run required plus selected experimental paths repeatedly and compute p50/p95.
Report warm realtime and durable archive verdicts separately even when they
share one launch.

The checked-in automation entrypoint is:

```bash
make session-propagation-sla
```

It wraps the required managed Codex warm realtime case with contaminated-run
retries and writes artifacts under `artifacts/session-propagation-sla/`. Exit
code `0` means pass, `1` means an SLA/tool failure, and `2` means the run was
contaminated by infrastructure/runtime transport conditions and should be
retried or bucketed separately. Exit code `3` means the runner is not set up
for managed-provider profiling and should fail the workflow as setup error.
The GitHub workflow must run on a configured dogfood runner with `longhouse`,
`longhouse-engine`, `codex`, Bun, and browser support available; generic
hosted runners are not valid for this SLA.

`SESSION_PROPAGATION_SLA_CASE`, `SESSION_PROPAGATION_PROFILE`,
`SESSION_PROPAGATION_PROVIDER`, and `SESSION_PROPAGATION_OWNERSHIP` select a
specific runnable case. The GitHub workflow uses those knobs to keep the
required gate and report-only experimental Codex cases in one CI pipeline.

Archive-path outliers should be debugged from `ship_trace.v1` before guessing.
The trace keeps total `prepare_ms` and also reports `prepare_open_db_ms`,
`prepare_binding_wait_ms`, and `prepare_parse_ms` when the current engine
emits them.

Release confidence:
Run one hosted warm-live and one hosted warm-close probe. Gate only on gross
fidelity failures until nightly distributions are boring.

Hostile cases:
Run PTY, SSH, network loss, process kill, sleep/wake, and machine offline as
monitoring, not as PR gates.
