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

## Current Required Path

`managed_codex_warm_live_graceful_close`

This is the only path currently promoted to `required`.

It proves the warm managed Codex happy path:

- managed session card appears with correct ownership and capability truth
- live output reaches timeline SSE and browser card on the live lane
- graceful bridge shutdown closes the card without waiting for a slow backstop
- durable archive catches up separately

Current target budgets are in `config/session-propagation-sla.toml`. The
important user-facing targets are:

- warm live output local truth to browser paint: 500ms target, 1000ms alarm
- warm graceful close local truth to browser paint: 1000ms target, 2000ms alarm
- durable archive local transcript to hosted events: 3000ms target, 30000ms alarm

## Experimental Paths

These should be measured and reported, but not treated as hard gates yet.

Managed Claude warm live and graceful close:
Claude uses native channel/MCP/stdin mechanics, not the Codex bridge. The
profiler must record channel state and process identity when it classifies a
Claude session.

Managed OpenCode lifecycle:
Lifecycle can be measured before remote send/interrupt is a product contract.

Unmanaged Codex and Claude graceful close:
These compatibility/import paths should be truthful, but they do not get the
same terminal-class realtime promise as managed control paths.

Managed Codex Ctrl-C, SSH disconnect, and provider kill:
These require a real PTY or SSH harness. The purpose is to distinguish terminal
relationship loss from provider process death.

## Undefined Paths

Unmanaged live send is not promised. The UI should not imply live control for
unmanaged sessions.

OpenCode remote send/interrupt is not promised until the managed control path
is defined.

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
save artifacts.

Nightly dogfood:
Run required plus selected experimental paths repeatedly and compute p50/p95.

Release confidence:
Run one hosted warm-live and one hosted warm-close probe. Gate only on gross
fidelity failures until nightly distributions are boring.

Hostile cases:
Run PTY, SSH, network loss, process kill, sleep/wake, and machine offline as
monitoring, not as PR gates.
