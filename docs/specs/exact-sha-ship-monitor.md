# Exact-SHA Foreground Ship Monitor

Status: Proposed
Last updated: 2026-04-14

## Problem

Multiple agents regularly push to `main` in close succession.

Today the failure mode is predictable:

- an agent pushes and says "done" before hosted deploy + QA actually finish
- if asked to verify later, the agent often inspects the latest branch run instead of the run for its own commit
- the user ends up manually mapping commits, workflow runs, demo state, and canary state

The repo already has most of the underlying verification primitives. What is missing is one agent-facing, foreground, exact-SHA ship command that blocks until the pushed commit is either fully green or clearly failed.

## Current-State Findings

### Good primitives already exist

- `.github/actions/wait-for-ci` already waits on an exact `head_sha` inside GitHub Actions.
- `scripts/ops/coolify-deploy.sh` is already a good blocking wait primitive with real exit status.
- `scripts/ops/deploy-status.sh` already reports live SHAs and health for:
  - demo runtime
  - hosted canary

It may also show hosted control-plane health for operator context. That state is
external to this public repo and is not a public deploy gate for this monitor.

### The local/agent boundary is the weak link

- There is no repo-local `ship-monitor` command today.
- The existing `zerg-ship` skill had examples that selected the latest workflow run instead of the run for the pushed SHA.
- `scripts/ci/run-on-ci.sh` still contains branch/latest selection logic that is safe for ad hoc CI dispatch, but not safe as a ship-verification pattern on a busy `main`.

### Live evidence from this repo on 2026-04-14

- Commit `d61f59e0e6...` had `contract-first-ci` and `runtime-image` green while `Deploy and Verify` was still running.
- At the same moment, `deploy-status.sh` showed:
  - demo runtime on `d61f59e0e6`
  - hosted canary still on prior SHA `dd7f4abc2b`
- That means "CI green" and even "demo updated" are still not enough to call a push done.

- Commit `bce7826f82...` had:
  - `contract-first-ci` failed
  - `Deploy and Verify` succeeded
- The deploy workflow skipped its "Wait for full CI gate" step because it classified the change as frontend-only.
- That means "deploy workflow green" is also not enough to call a push done.

## Goals

- One blocking foreground command after `git push`
- Exact commit targeting by SHA, never branch-latest inference
- Correct behavior with multiple agents pushing to the same repo and branch
- Automatic handling of varying workflow sets based on what GitHub actually triggered for that SHA
- A clear terminal state:
  - success
  - failure
  - timeout
  - later: superseded
- Live deploy verification after workflow success
- Human-readable output plus machine-readable JSON

## Non-Goals

- Rebuilding Longhouse product surfaces around shipping before the repo-local path exists
- Relying on hidden background daemons
- Forcing a global shell wrapper around every `git push` on the machine
- Perfect stale-run cancellation in the first iteration

## Option Set

### Option A: Guidance-only

Update prompts/skills and tell agents to manually check exact-SHA runs.

Pros:

- Fastest
- No new code

Cons:

- Still depends on model compliance
- Still leaves multi-step manual monitoring logic in every session
- Does not give the user one command with a real exit code

### Option B: Repo-local `ship-monitor` command

Add a blocking command that watches the exact pushed SHA through GitHub Actions and live deploy verification.

Pros:

- Solves the immediate user pain
- Stays repo-local and easy to adopt
- Can be called from any agent as one foreground tool call
- Gives real exit codes and a stable JSON summary

Cons:

- Agents still need to call this command unless a wrapper is added

### Option C: Repo-local `ship` wrapper

Wrap `git push` plus exact-SHA monitoring in one command.

Pros:

- Best operator UX inside this repo
- Easier to teach agents: "ship it" maps to one command

Cons:

- Still repo-local
- Needs a good `ship-monitor` first

### Option D: GitHub-side stale-run collapse

Use `check-freshness` and/or workflow `concurrency` to stop stale SHAs from chewing through CI/deploy capacity.

Pros:

- Reduces queue noise
- Lowers wasted deploy work on busy `main`

Cons:

- Introduces a new terminal state: superseded
- Needs careful coordination with any local monitor
- Can make earlier agents look "failed" unless the monitor understands supersession

### Option E: Longhouse-native ship jobs

Attach ship state directly to sessions and surface it in Longhouse.

Pros:

- Strongest long-term UX

Cons:

- Bigger product project
- Not the fastest path to fixing today's repo pain

## Recommended Path

### Phase 1: Build `ship-monitor`

Add a repo-local command:

```bash
./scripts/ops/ship-monitor.sh --sha <full-sha>
```

Suggested make target:

```bash
make ship-watch SHA=<full-sha>
```

Core behavior:

1. Resolve target SHA, branch, and repo.
2. Query GitHub for push-triggered workflow runs for that exact SHA:

   ```bash
   gh run list --commit "$SHA" --event push --json workflowName,databaseId,status,conclusion,url,createdAt
   ```

3. Use a short settle window so the full run manifest appears before monitoring begins.
4. Track the exact run IDs for that SHA until all tracked workflows reach terminal state.
5. Treat the result as failed if any blocking workflow for that SHA fails.
6. If deploy workflows succeeded, verify live surfaces with `deploy-status.sh`.
7. Emit:
   - readable summary
   - JSON summary
   - exit code

### Why actual workflow discovery beats local path prediction

The user wants one system that handles different work types.

Local path prediction sounds attractive, but the repo already has path filters, skipped jobs, and workflow-internal gates that do not map cleanly to one static rule set. The cleaner first move is:

- discover what GitHub actually triggered for the target SHA
- wait on that exact manifest
- keep only a small explicit ignore list if needed later

That avoids duplicated lane logic in local scripts.

### Blocking policy for v1

Default v1 policy:

- every push-triggered workflow that appears for the target SHA is blocking
- except an explicit ignore list if the repo proves it needs one later

This is intentionally stricter than the current deploy workflow gates. The goal is to prevent "deploy green, hidden CI failure" from being reported as success.

### Deploy verification for v1

If `Deploy and Verify` ran and succeeded:

- demo runtime SHA must match target SHA
- hosted canary SHA must match target SHA
- demo runtime health must be healthy
- hosted canary health must be healthy

`deploy-status.sh` already exposes enough state for a first pass.

### Suggested exit codes

- `0`: success
- `10`: a tracked workflow failed
- `11`: timeout waiting for workflow completion
- `12`: no matching push workflows were found for the target SHA
- `13`: deploy drift or unhealthy live surface after workflows passed

### Suggested output shape

```json
{
  "repo": "cipher982/longhouse",
  "target_sha": "d61f59e0e6a8319a0909653ac840156b0fc2aba3",
  "result": "success",
  "workflows": [
    {
      "name": "Deploy and Verify",
      "run_id": 24412067834,
      "status": "completed",
      "conclusion": "success",
      "url": "..."
    }
  ],
  "live": {
    "demo_sha": "d61f59e0e6",
    "canary_sha": "d61f59e0e6"
  }
}
```

## Phase 2: Add `ship`

After `ship-monitor` works well, add:

```bash
./scripts/ops/ship.sh
```

Behavior:

- run `git push`
- resolve `HEAD`
- call `ship-monitor` in the foreground

This becomes the repo-native answer to "ship it".

It should stay thin. Do not hide a large preflight matrix behind it in v1.

## Phase 3: Add Superseded-SHA Semantics

After the monitor exists, wire stale-run handling on the GitHub side.

Possible mechanisms:

- use `.github/actions/check-freshness`
- add workflow `concurrency` where cancellation is safe

Once stale runs can terminate early, `ship-monitor` should recognize:

- `superseded`
- optionally later: `included_in_green_descendant`

Do not start here. The monitor should exist first.

## Open Questions

- Should a newer successful descendant SHA count as success for an earlier agent whose commit is an ancestor?
- Do we want any push workflows to be explicitly advisory instead of blocking?
- Should `ship` eventually support `--json` and `--open-failure-logs` helpers for agent UX?
- Do we want a narrower parser/helper for hosted canary exact-SHA verification?

## Immediate Next Moves

1. Implement `scripts/ops/ship-monitor.sh` as the exact-SHA blocking primitive.
2. Add `make ship-watch`.
3. Update `zerg-ship` to instruct agents to use `ship-monitor`, not ad hoc `gh run list` calls.
4. After that lands, add `scripts/ops/ship.sh` as the thin push + monitor wrapper.
5. Only then decide whether to add stale-run cancellation and superseded-state handling.
