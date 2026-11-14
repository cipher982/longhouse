---
name: zerg-ship
description: Land and ship a Longhouse change (the `cowbell` flow) - push an exact SHA, watch canary qualification, read Hosted Live QA, decide whether a CLI release, TestFlight build or local dogfood refresh is also needed. Use when pushing to main, answering "is it deployed", or promoting or releasing by hand.
---

# Ship a change

The workspace `AGENTS.md` owns the ring table and the landing flow (worktree,
`make validate`, `check-push-readiness`). This skill owns which command moves
which surface and how to read the result.

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
2. Before pushing: `make affected-check BASE=<base-sha>` shows which CI lanes the
   diff hits. Run the focused tests for the changed behavior (a CSS change needs a
   web proof, not engine tests), then `make check-push-readiness`. Once per clone:
   `make install-precommit-autofix`.
3. `make ship SHA=<full-sha>` pushes that exact SHA, prints CI queue load and waits
   on the push-triggered runs (`runtime-image.yml`, `deploy-and-verify.yml`, and
   others by touched path). Read the start banner to confirm SHA and subject, stay
   in the foreground, and do not wrap it in a short timeout.
4. From a topic worktree, rebase onto `origin/main` first and pass
   `ARGS="--branch main"`; otherwise `ship` pushes the topic branch and nothing
   deploys. `ARGS=--json` gives one blocking verdict.
5. Already pushed: `make ship-watch SHA=<full-sha>` (`ARGS="--poll 30"`).
6. A non-zero exit means ship failed. Explain suspected causes, but do not report
   it as success.

Waiting rules: use a completion event or blocking wait once and move on; no
`pgrep`/`curl` loops. For a queued GitHub run use
`gh run watch <id> --interval 60 --compact --exit-status` (the 3 s default hits
the API rate limit when several watchers run).

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
  non-superseded verdict gates production. Dispatch command: workspace Rings table.
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
ring, and the targets refuse while a Promote Rings run is active.

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
of production promotion and runs from any checkout (details in AGENTS.md
"Shipping").

`make launch-readiness SHA=<full-sha>` is the stricter exact-SHA check after the
hosted and release lanes ran: it also compares the latest release and PyPI build
identity. It requires the demo to serve the SHA unless `ARGS=--skip-demo`; the
demo is retired, so pass that flag (`release.sh` always does).

## TestFlight

`make testflight [SHA=<sha on origin/main>] [WHATS_NEW="note"] [INTERNAL_ONLY=1] [DRY_RUN=1]`
dispatches `ios-testflight.yml` on wisp (archive, upload, wait for processing).
`INTERNAL_ONLY=1` skips the public group and Beta App Review; `DRY_RUN=1` archives
and exports only. `make testflight-status` shows builds, review state and the
public link (needs `ASC_API_*` in the environment). Public publishing fails when
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
`cd server && uv tool install -e .` instead.

## Report

Give the task SHA, runtime disposition (`deployed` or `no_runtime_change`), the
workflow run and receipt IDs from the verdict, the Hosted Live QA verdict (or
"pending"/"failed"), and which local refresh, release or device install was
needed. Claim production only after `make promote-production` (or Promote Rings)
reported its receipt. If nothing deployed, do not say the canary changed.
