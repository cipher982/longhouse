---
name: zerg-ship
description: Land and ship a Longhouse change (the `cowbell` flow) - push an exact SHA, watch canary qualification, read Hosted Live QA, decide whether a CLI release, TestFlight build or local dogfood refresh is also needed. Use when pushing to main, answering "is it deployed", or promoting or releasing by hand.
---

# Ship a change

Longhouse has no users, so landing is the whole job and downtime on a hosted
surface costs nothing but time. This skill owns the rings, how to land in the
shared checkout, which command moves which surface and how to read the result.

## Rings

| Ring | Surface | Moves when |
|---|---|---|
| Canary | `release-canary-a` | every `main` push touching runtime paths (`server/`, `web/`, `engine/`, `config/`, runtime Dockerfile) |
| QA (not a ring) | `hosted-qa-a` | Hosted Live QA deploys each published commit, one run at a time |
| Dogfood | the maintainer's own tenant | every published runtime image of `main`, automatically (Promote Rings) |
| Production | other hosted tenants and the new-tenant image | the exact image dogfood serves, once its gates pass; no tag needed |
| Laptops | Mac app, CLI, engine | only from `make release VERSION=vX.Y.Z` |

Promote Rings (`.github/workflows/promote-rings.yml`) runs after every image,
archive, canary, QA and CI completion, and on a Sauron wake every 10 minutes
while a ring trails. It never moves backwards. Its decision per ring is a commit
status (`longhouse/dogfood`, `longhouse/production`); `make promote-rings` runs
it now. Sauron pages when David's machines stop shipping after a dogfood
cutover, and when production trails dogfood for 8 hours. Promote Rings is the
only automatic writer of dogfood and production; `make release` and by-hand
promotions take an expiring ring lock (`python3 scripts/ops/ring_lock.py
status`). Before cutting a release, check `origin/main` still equals the
candidate SHA.

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
- A newer push supersedes queued runs; Deploy and Verify then stands down green
  naming the covering commit.
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
| `server/`, `web/`, `engine/`, `config/`, runtime Dockerfile | Hosted runtime | A `main` push builds the image and qualifies the canary. Dogfood and production follow by themselves (Promote Rings). |
| `longhouse` CLI, hooks, installer, `longhouse machine repair`, engine, Desktop App | CLI/package release | `make release VERSION=vX.Y.Z` ([RELEASE.md](../../../RELEASE.md)). A hosted deploy does not reach users' machines. Say so; do not report "deployed". |
| `ios/` | TestFlight | `make testflight` (below). Never a hosted deploy. |
| The separately installed runner | Runner release | `runner-release.yml`; its own path. |
| Control plane | Not this repo | `~/git/zerg/control-plane`, its README ("Production activation and rollback"). |

A commit that spans lanes needs each lane run and verified on its own.

## Land and watch (`cowbell`)

1. Pick the task SHA: commit uncommitted work now, otherwise the latest commit
   that represents the task, even if already pushed.
2. Before pushing: run the focused tests for the changed behavior (a CSS change
   needs a web proof, not engine tests). `make ship` then runs the pre-push gate
   itself; `make prepush-gate [HEAD=<sha>] [DRY=1]` runs or previews it by hand.
   The gate (`scripts/ci/prepush_gate.py`) resolves the committed diff
   `origin/main...HEAD` (never your working tree, never another session's dirty
   files) to the lanes CI runs for those paths, checks the exact commit out in a
   clean directory and runs them on the `ci` VM through `scripts/ops/remote.sh`:
   `make validate`, provider contract, managed launch lifecycle, backend tests
   selected by Python import graph (`scripts/ci/backend_select.py`; the full suite
   for schemas, config, models and shared fixtures), engine and shipper tests, the
   frontend lane, wheel packaging and the Terminal Truth harness selfcheck. rustfmt
   runs locally in a second. It prints lane times and the log tail of every failed
   lane, and exits 1 so the push does not happen. It cannot run the real-provider
   Terminal Truth cells (no provider CLI runs on the laptop or the guest); the
   post-push run and the repair bot cover those, and `make prove CELLS="claude/helm:tool"
   SHA=<sha>` runs any of them against your commit on cube-bench in about a minute
   (`zerg-testing` skill). `make affected-check GATE=1` shows
   the plan without running it. Once per clone: `make install-precommit-autofix`
   installs the pre-push hook for every worktree, so a plain `git push origin
   HEAD:main` is gated too. `LONGHOUSE_PREPUSH_GATE=skip` (or `ARGS=--no-gate`)
   bypasses it and says nothing was checked; use it only for a revert that must
   land now.
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

When main goes red anyway: the workflow `main-repair.yml` runs when CI or Terminal
Truth fails on a main push (`scripts/ci/main_repair.py`). It acts only if that run
is the newest non-cancelled run of its workflow (a newer green run containing the
commit means someone already fixed forward; a newer run in progress reports for
itself), reruns the failed jobs once and only acts on a second failure, finds the
range since the last green ancestor, bisects it by running the failing check (a
Terminal Truth failure is attributed only when exactly one commit in the range
touches its paths), and reverts the culprit with a fast-forward push of
`refs/heads/main`. It never reverts a revert or a commit marked `[no-auto-revert]`,
stops after two automatic reverts in six hours, abstains on a conflicting revert,
and never touches tags. The revert commit says why and carries a log excerpt; the
Sauron job `longhouse-ship-watch` relays it to the session that shipped the culprit.
Re-land your change with the fix on top. Dry run against any red run:
`GITHUB_TOKEN=$(gh auth token) python3 scripts/ci/main_repair.py --run-id <id> --dry-run`.

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

- `deploy-and-verify.yml` waits for the exact-SHA image, reprovisions the canary
  and runs a fast smoke. The canary is shared: a run whose canary a newer push
  replaced ends green as *superseded* ("Stand down, canary superseded"). That is
  not a verdict on your SHA; check the newest commit's run. A newer head that
  publishes no runtime image (a CI or docs push) does not supersede: the deploy
  waits on that head's CI, which contains your SHA, and deploys your image.
- Hosted Live QA (`hosted-live-qa.yml`) is dispatched per qualified SHA and runs
  daily at 10:41 UTC. It deploys the exact commit to its own instance and records
  a verdict per SHA. "Did QA pass this SHA" is that verdict; a passed,
  non-superseded verdict gates production. Dispatch by hand with
  `gh workflow run hosted-live-qa.yml -f source_sha=<full-sha>`.
- `make qa-live` is not a developer command; it refuses personal and dogfood
  targets and needs a dedicated QA instance and token.
- To state what a host runs: `make deploy-status` or
  `curl -sL https://<host>/api/health` (`build.commit`).
- To release a held push by `workflow_dispatch`, dispatch `runtime-image.yml` and
  `deploy-and-verify.yml` at the same exact SHA.
- Never deploy a hosted tenant by direct SSH or Compose; it bypasses the
  deployment receipt and target fencing.

## Promoting by hand

Promote Rings does this automatically. By hand only to roll back or unstick a
ring, and the targets refuse while a Promote Rings run is active. Both need
`CONTROL_PLANE_ADMIN_TOKEN` (`control-plane/docs/runbooks/hosted-operations.md`). State the full
SHA and the commits it sweeps in.

- `make promote-dogfood SHA=<full-sha> [FAST_LANE=1] [SUBDOMAIN=...]` needs a
  canary-qualified Deploy and Verify run for the SHA. The fast lane is allowed
  only when no path between the served SHA and the target matches
  `scripts/ops/dogfood-sensitive-paths.toml` (auth, migrations and similar).
- `make promote-production [SHA=<full-sha>] [CHECK=1]` moves production tenants and
  the new-tenant image to the image dogfood serves (default when `SHA` is omitted).
  `CHECK=1` prints the gate receipt and moves nothing; `VERSION=` is refused.
  After a halted tenant wave, rerun with `PROMOTION_ATTEMPT=<n+1>`. The retired
  demo is skipped unless `DEMO_ENABLED=1` with `DEMO_SSH_HOST` and `DEMO_ENV_PATH`.
- `make reprovision SUBDOMAIN=<name> IMAGE=ghcr.io/cipher982/longhouse-runtime@sha256:<digest>`
  submits a durable deployment through the control-plane API. Always pass
  `SUBDOMAIN` (the fallback is `demo`). Tenant data survives.
- For tenant logs and state, start with `scripts/ops/hosted-session-debug.sh` and
  the `zerg-hosted-debug` skill; they are read-only diagnostics.

## CLI release and launch readiness

`make release VERSION=vX.Y.Z` publishes the native pair (`longhouse`,
`longhouse-engine`), `Longhouse.app` and the PyPI wheel. Existing users get it by
rerunning `scripts/install.sh` or `uv tool upgrade longhouse`. It is independent
of production promotion and runs from any checkout (details in
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
workflow run and receipt IDs from the verdict, the Hosted Live QA verdict (or
"pending"/"failed"), and which local refresh, release or device install was
needed. Claim production only after `make promote-production` (or Promote Rings)
reported its receipt. If nothing deployed, do not say the canary changed.
