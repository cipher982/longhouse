---
name: zerg-ship
description: Land and ship a Longhouse change (the `cowbell` flow) - push an exact SHA, watch the Release workflow, read Hosted Live QA, decide whether a CLI release, TestFlight build or local dogfood refresh is also needed. Use when pushing to main, answering "is it deployed", or promoting or releasing by hand.
---

# Ship a change

Longhouse has no users, so landing is the whole job and downtime on a hosted
surface costs nothing but time. This skill owns the rings, how to land in the
shared checkout, which command moves which surface and how to read the result.

## Rings

| Ring | Surface | Moves when |
|---|---|---|
| Canary | `release-canary-a` | every green `main` commit whose range changes a runtime input (Release workflow) |
| QA (not a ring) | `hosted-qa-a` | Release runs Hosted Live QA there when the range touches a dogfood-sensitive path |
| Dogfood | the maintainer's own tenant | after the canary (and QA when sensitive), automatically |
| Production | `longhouse.ai` | dark: no deploy target. Added to `release.yml` when it launches |
| Laptops | Mac app, CLI, engine | only from `make release VERSION=vX.Y.Z` |

The Release workflow (`.github/workflows/release.yml`) starts when `CI` completes
green on `main` and does everything in one run: plan, build the image once,
archive it, canary deploy and smoke, QA (sensitive ranges), dogfood deploy and
smoke. `scripts/ops/release_plan.py` holds the gates: green CI, never backwards
or sideways, nothing to do when no runtime input changed, QA when the range
matches `scripts/ops/dogfood-sensitive-paths.toml`, and a Terminal Truth verdict
when a live-preview path changed. One release runs at a time and is never
cancelled; a newer green commit replaces a waiting one. A red release leaves
dogfood where it was. Sauron pages when David's machines stop shipping after a
dogfood cutover. `make release` takes an expiring ring lock (`python3
scripts/ops/ring_lock.py status`). Before cutting a release, check `origin/main`
still equals the candidate SHA.

## Landing in the shared checkout

David and several agents work in `~/git/zerg/longhouse` at once.

- Make code changes in a worktree:
  `git -C ~/git/zerg/longhouse worktree add ~/git/_wt/longhouse-<name> -b <name> origin/main`,
  and `touch .wt-keep` while it is live.
- Land with
  `git fetch && git rebase origin/main && make validate && make check-push-readiness && git push origin HEAD:main`.
  Chain gates with `&&`, never `;`, and never pipe a gate into `tail` or `grep`
  before `&&` (the pipe hides its exit status). Then remove `.wt-keep`, the
  worktree and the branch.
- `git status` first; commit explicit paths (`git commit -o <paths>`), never
  `git add .` or `-A`. Never reset or force-push `main`; roll forward.
- Pre-commit fixes mechanical problems (format, whitespace, the provider
  contract digest and census) and stages the fix into the same commit; `git show
  --stat HEAD` shows what it added. It refuses when the file also has unstaged
  changes. It never autofixes lint.
- Never run a `--fix` linter over a directory (it deletes pytest fixtures it
  thinks are unused); format only files you edited. `rustfmt <file>` follows
  `mod` declarations, so check `git status` after it.
- CI is shared and runs on David's runners (Linux lanes on `ci-*`, iOS on
  `wisp`). Prove changes locally with the smallest test; never push or rerun just
  to see whether CI passes. When `make ship` reports the queue SATURATED, batch
  commits. A rare `TLS handshake timeout` on a cube runner is usually the home
  WAN, not Longhouse (`control-plane/docs/runbooks/hosted-operations.md`).
- A newer push supersedes a queued CI run and a waiting Release run; the newer
  commit contains yours, so its release ships your change.
- While `main` is red, one agent claims the fix, lands it and posts the SHA;
  nobody else reruns. Diagnose a failing check before owning it.
- Anchor every CI and deploy claim to an exact SHA, run id and live surface;
  "latest" may belong to another agent.
- Call out schema changes and new required environment variables. For catalog,
  storage or transport changes, prove the deploy on a real user route
  (machines, timeline, detail), not just health.
- Message other sessions only for a real conflict, blocker or handoff.

## Which lane a change needs

| Change touches | Lane | What moves it |
|---|---|---|
| `server/`, `web/`, `engine/`, `config/`, runtime Dockerfile | Hosted runtime | Green CI on `main` starts Release: image, canary, QA when sensitive, then dogfood, by itself. |
| `longhouse` CLI, hooks, installer, `longhouse machine repair`, engine, Desktop App | CLI/package release | `make release VERSION=vX.Y.Z` ([RELEASE.md](../../../RELEASE.md)). A hosted deploy does not reach users' machines. Say so; do not report "deployed". |
| `ios/` | TestFlight | `make testflight` (below). Never a hosted deploy. |
| The separately installed runner | Runner release | `runner-release.yml`; its own path. |
| Control plane | Not this repo | `~/git/zerg/control-plane`, its README ("Production activation and rollback"). |

A commit that spans lanes needs each lane run and verified on its own.

## Land and watch (`cowbell`)

1. Pick the task SHA: commit uncommitted work now, otherwise the latest commit
   that represents the task, even if already pushed.
2. Before pushing: run the focused tests for the changed behavior (a CSS change
   needs a web proof, not engine tests), then push. Pushes are not gated: CI runs
   every lane after the push, and a red main is fixed forward (the repair bot
   helps). rustfmt runs at commit as a pre-commit autofix. The old pre-push gate
   (`scripts/ci/prepush_gate.py`: the CI lanes for your diff on the `ci` VM) is
   opt-in for when you want a pre-flight: `make prepush-gate [HEAD=<sha>] [DRY=1]`,
   `make ship ARGS=--gate`, or `LONGHOUSE_PREPUSH_GATE=run git push`. Real-provider
   Terminal Truth cells run on `cube-proof` with `make prove CELLS="claude/helm:tool"
   SHA=<sha>` (`zerg-testing` skill).
3. `make ship SHA=<full-sha>` pushes that exact SHA to main, registers a watch and
   returns in seconds. Move on. The Sauron job `longhouse-ship-watch` on clifford
   follows the SHA through CI, canary deploy and Terminal Truth and sends a message
   to this session (`$LONGHOUSE_MANAGED_SESSION_ID`) only when a run fails or the
   deadline (default 60 min, `DEADLINE_MIN=N`) passes with runs unfinished or never
   started. The message carries the failing run URL and a log excerpt. Silence means
   green or covered: a failure already fixed forward by a newer green main run that
   contains the commit wakes nobody. The watch is the ref
   `refs/ship/<sha>/<session>/<pushed-epoch>/<deadline-epoch>` (no workflow runs on
   it); the job writes `refs/ship-done/<sha>` when finished and prunes both after
   two weeks.
4. `make ship WAIT=1 SHA=<full-sha>` keeps the old blocking behavior (foreground
   until a verdict); use it for a release candidate or when the next step needs the
   deployed result. From a topic worktree, rebase onto `origin/main` first and pass
   `ARGS="--branch main"`; otherwise `ship` pushes the topic branch and nothing
   deploys (a topic-branch ship always blocks).
5. Already pushed and you want to watch: `make ship-watch SHA=<full-sha>`
   (`ARGS="--poll 30"`).
6. Landing is optimistic. When another agent pushed first, `ship` replays your
   commits on the newest `origin/main` in memory (`git merge-tree --write-tree` and
   `git commit-tree`; no working tree or index is touched) and pushes that, retrying
   a lost race up to five times. It fails (exit 5, nothing pushed) only on a real
   conflict and names the files. The commit that lands has a new SHA; the watch
   follows that one. Your local branch keeps the original commits; `git fetch &&
   git rebase origin/main` drops them as already applied.
7. Without WAIT, a zero exit means the push stood, not that CI passed. A non-zero
   exit means ship failed; do not report it as success.

When main goes red anyway: nothing reverts it. The Sauron job `longhouse-ship-watch` messages the
session that shipped each commit the red CI or Terminal Truth run covers (a cancelled run is followed to
the run that contains the commit), with the failing run, jobs and a log excerpt. Fix forward with a new
commit. If the notice says the failure is infrastructure (full runner disk, lost runner, network timeout,
rate limit), rerun the failed jobs with `gh run rerun --failed <run-id>` instead.

Waiting rules: use a completion event or blocking wait once and move on; no
`pgrep`/`curl` loops. Never run `gh run watch` (it polls every 3 s, and even with
`--interval 60` several watchers add up) and never poll GitHub more often than
every 45 s. All personal GitHub tokens share one 5000/h budget; on 2026-11-15 agent
watchers exhausted it and blocked every `gh` call for 15 minutes. Use
`make ship-watch SHA=<sha>` (ETag-cached, 45 s floor), or hand the SHA to the
Sauron `longhouse-ship-watch` job and wait for its message: `make ship` already
leaves the ref that makes it wake the session on a failure or a missed deadline.
Check the budget with `gh api rate_limit` (free).

Reading the result:

- Release runs are titled `Release <full sha>` (a run started by CI completing
  may carry another head SHA of its own). A docs-only or already-served commit
  runs only the Plan job and ends green: nothing deployed, and the summary says
  why. Do not say the canary changed in that case.
- Hosted Live QA is a job of Release, run only for sensitive ranges (or by hand
  with `-f qa=true`). It deploys the exact commit to its own instance and
  records a verdict (passed, superseded or failed); dogfood proceeds only on
  `passed`.
- `make qa-live` is not a developer command; it refuses personal and dogfood
  targets and needs a dedicated QA instance and token.
- To state what a host runs: `make deploy-status` or
  `curl -sL https://<host>/api/health` (`build.commit`).
- To release a commit by hand: `gh workflow run release.yml -f sha=<full sha>`
  (canary only), add `-f dogfood=true` to continue to dogfood.
- Never deploy a hosted tenant by direct SSH or Compose; it bypasses the
  deployment receipt and target fencing.

## Moving a surface by hand

Release does this automatically. By hand only to roll back or unstick.
`CONTROL_PLANE_ADMIN_TOKEN` is needed for `make reprovision`
(`control-plane/docs/runbooks/hosted-operations.md`). State the full SHA and the
commits it sweeps in.

- `make rollback SHA=<full-sha>` runs Release for that commit with
  `rollback=true`: image reused, canary, QA gate, dogfood. Read RELEASE.md
  "Rollback" first: do not roll dogfood back across a schema migration.
- `make reprovision SUBDOMAIN=<name> IMAGE=ghcr.io/cipher982/longhouse-runtime@sha256:<digest>`
  submits a durable deployment through the control-plane API. Always pass
  `SUBDOMAIN` (the fallback is `demo`). Tenant data survives.
- For tenant logs and state, start with `scripts/ops/hosted-session-debug.sh` and
  the `zerg-hosted-debug` skill; they are read-only diagnostics.

## CLI release and launch readiness

`make release VERSION=vX.Y.Z` publishes the native pair (`longhouse`,
`longhouse-engine`), `Longhouse.app` and the PyPI wheel. Existing users get it by
rerunning `scripts/install.sh` or `uv tool upgrade longhouse`. It is independent
of the Release workflow and runs from any checkout (details in
[RELEASE.md](../../../RELEASE.md)). It runs from a disposable checkout of
`origin/main`, holds the release ring lock, validates the bump commit under the
heavy-build lock, waits for the exact-SHA gates and notarizes; a rerun resumes.

`make launch-readiness SHA=<full-sha>` is the stricter exact-SHA check after the
hosted and release lanes ran: it also compares the latest release and PyPI build
identity. It requires the demo to serve the SHA unless `ARGS=--skip-demo`; the
demo is retired, so pass that flag (`release.sh` always does).

## TestFlight

`make testflight [SHA=<sha on origin/main>] [WHATS_NEW="note"] [INTERNAL_ONLY=1] [DRY_RUN=1]`
dispatches `ios-testflight.yml` on wisp (archive, upload, wait for processing).
`INTERNAL_ONLY=1` skips the public group and Beta App Review; `DRY_RUN=1` archives
and exports only. `make testflight-status` shows builds, review state and the
public link, https://testflight.apple.com/join/CmEd5kY7 (needs `ASC_API_*` in
the environment, and the iOS 26 SDK to build; spec
`control-plane/docs/specs/ios-testflight-distribution.md`). Public publishing fails when
the group's link differs from the one in
`web/src/features/marketing/landing/links.ts`. If `ios/` changed: build, look at a
rendered simulator frame (`zerg-ui`), then `make phone-deploy` as the last step. Do
not ask David to build in Xcode.

## Local dogfood refresh

A hosted ship does not update this Mac. After a successful ship that changed the
CLI, engine, Desktop App, installer, or hooks:

```bash
make dogfood-refresh            # installs exactly origin/main (SHA=<rev> for another)
launchctl kickstart -k gui/$(id -u)/ai.longhouse.app
```

Then confirm the machine still ships (`longhouse-server sessions list --limit 1`
shows fresh activity). Skip it for web-only, docs-only, test-only and
hosted-runtime-only changes. Python-CLI-only edits under `server/zerg/cli/` can use
`cd server && uv tool install -e .` instead. After the refresh, check this Mac
still ships.

## Report

Give the task SHA, runtime disposition (`deployed` or `no_runtime_change`), the
Release run URL, the Hosted Live QA verdict (or "skipped"/"pending"/"failed"),
the commit dogfood serves (its `/api/health` build commit),
and which local refresh, release or device install was needed. There is no
production deploy. If nothing deployed, do not say the canary changed.
