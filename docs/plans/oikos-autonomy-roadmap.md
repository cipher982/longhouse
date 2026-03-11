# Oikos Autonomy Roadmap

Status: active
Owner: David / Oikos
Started: 2026-03-10
Last Updated: 2026-03-11

## Goal

Build a realistic harness for proactive Oikos so we can dogfood, evaluate, and improve autonomy behavior without making David the manual QA loop for every change.

This roadmap tracks both:

- long-term product/harness phases
- near-term concrete steps that should move in small commits

Keep this document current as the work evolves.

## Guiding Constraints

- Start simple and dogfood.
- Prefer realistic journeys over isolated prompt trivia.
- Reuse existing Longhouse session history and artifacts instead of duplicating them.
- Build shadow mode before broad autonomous action.
- Favor comparable results and saved artifacts over ad hoc screenshots or anecdotes.
- Keep implementation future-friendly: better models should simplify the system, not invalidate it.

## Long-Term Phases

## Phase 1: Shadow Journey Harness

Objective: let Oikos wake on synthetic/recorded journey inputs, decide what it would do, and save that decision without taking real action.

Success looks like:

- journey cases are defined as durable fixtures
- a runner can execute them repeatably
- results include trigger, context packet, decision, rationale, and artifacts
- assertions can check both hard invariants and behavior quality

## Phase 2: Bounded Local Actions

Objective: allow one or two safe actions from the same harness, likely inspect/continue/escalate.

Success looks like:

- shadow mode and act mode share the same journey structure
- bounded actions are auditable
- regressions show up in hermetic tests before live dogfood

## Phase 3: Browser and Hosted Smokes

Objective: prove the real app wiring around autonomy journeys works outside hermetic backend evals.

Success looks like:

- local/browser smoke for one or two canonical journeys
- hosted smoke against david010
- clear artifacts for failure triage

## Phase 4: Dogfood Feedback Loop

Objective: convert real-world Oikos wins/failures into reusable eval fixtures and acceptance cases.

Success looks like:

- real autonomy decisions are logged and reviewable
- good/bad cases become eval fixtures
- changes can be judged against prior behavior, not memory

## Phase 5: Broader Trigger Surface

Objective: expand beyond the first coding-session triggers only after the loop is trustworthy.

Possible later additions:

- CI/deploy wakeups
- runner availability changes
- project-level sweeps
- multi-session coordination

## Near-Term Ring

These are the active next steps. Keep this list short and current.

- [x] Add roadmap/spec links to the main task tracker without clobbering unrelated work
- [x] Implement a backend autonomy shadow runner that can execute journey cases without taking real actions
- [x] Define the first journey dataset around coding-session wakeups
- [x] Add autonomy-specific assertions for decision class, action count, forbidden actions, and artifact/log presence
- [x] Add a make target or documented command for the autonomy eval ring
- [x] Convert the first real dogfood observations into new journey fixtures
- [x] Wire the first live operator wakeup set around coding-session transitions plus a periodic sweep fallback
- [x] Add the thinnest user-backed operator policy surface
- [x] Spec and land a phase-1 wakeup-history ledger for suppressed / enqueued / failed wakeups
- [x] Attach post-run ignored / acted outcome classification to the wakeup ledger
- [x] Ship the first bounded local action behind the same policy surface

## Current Runtime Ring

What is live today:

- `presence.blocked`
- `presence.needs_user`
- `periodic_sweep`
- recent post-ingest `session_completed`

Current guardrails:

- no raw `idle` / Stop wakeups
- no historical backfill completion wakeups
- no completion wakeup if fresh presence already shows resumed or paused work
- all live wakeups respect both the env master switch and user-backed `preferences.operator_mode`

Current durable evidence outside runs:

- the wakeup ledger now captures `suppressed`, `enqueued`, `ignored`, `acted`, and enqueue/run-time `failed` outcomes
- `acted` currently means Oikos launched a follow-up path, with `continue_session` as the first bounded sanctioned action

## First Journey Set

The initial cases should stay small and high-signal:

- [x] session completed, nothing to do
- [x] session completed, obvious next step
- [x] session blocked on real human fork
- [ ] session blocked on small bounded follow-up
- [x] session needs user, low priority
- [x] periodic sweep with no meaningful work
- [x] trigger storm / duplicate wakeups
- [ ] two sessions competing for attention

## Evidence Contract

Every journey run should leave enough evidence to answer:

- what woke Oikos up
- what context packet it saw
- what it decided
- what it would have done or actually did
- why the test passed or failed

The live runtime should leave enough evidence to answer:

- which wakeups were suppressed before a run existed
- which wakeups produced a run
- which wakeups failed at enqueue / transport time

At minimum, save:

- case id
- trigger payload
- compact context packet
- decision payload/result
- assertion outcomes
- any generated artifacts/log files

## Notes

- Existing repo surfaces to reuse:
  - backend eval harness under `apps/zerg/backend/evals/`
  - targeted backend tests under `apps/zerg/backend/tests_lite/`
  - local/browser E2E and smoke flows under `apps/zerg/e2e/`
  - hosted smoke via `scripts/qa-live.sh`
- Keep the harness independent enough that it can start before the full proactive runtime exists.
- The first slice is about learning whether Oikos makes sensible decisions, not shipping a full autonomy control plane.
- 2026-03-10: Foundation slice landed as a deterministic shadow harness:
  - `apps/zerg/backend/zerg/services/oikos_autonomy_journeys.py`
  - `apps/zerg/backend/tests_lite/fixtures/oikos_autonomy_journeys.yml`
  - `apps/zerg/backend/tests_lite/test_oikos_autonomy_journeys.py`
  - `make test-autonomy-journeys`
- 2026-03-10: Journey fixtures now cover the first live operator-mode trigger observations too:
  - `needs_user` low-priority pauses stay parked
  - duplicate blocked wakeups are ignored instead of creating churn
- 2026-03-10: The first live runtime wakeup set landed:
  - `presence.blocked`
  - `presence.needs_user`
  - builtin `periodic_sweep`
  - post-ingest recent `session_completed`
- 2026-03-10: The first thin policy state landed without a new table:
  - live wakeups now respect `User.context["preferences"]["operator_mode"]`
  - `OIKOS_OPERATOR_MODE_ENABLED` remains the global master switch
- 2026-03-11: The next spec slice is no longer "more triggers." It is wakeup history:
  - we need a tiny durable ledger for wakeup handling before broader actions or UI
- 2026-03-11: Phase 1 of the wakeup ledger is intentionally narrower than the end state:
  - persist `suppressed`, `enqueued`, and enqueue-time `failed`
  - defer `ignored` / `acted` until the first bounded action path exists
- 2026-03-11: The first bounded operator action path is now live:
  - Oikos is taught to continue the same coding session via `spawn_workspace_commis(..., resume_session_id=...)`
  - operator-surface continuation is hard-gated by `preferences.operator_mode.allow_continue`
  - wakeup rows now finalize to `ignored`, `acted`, or `failed` after the run outcome is known
