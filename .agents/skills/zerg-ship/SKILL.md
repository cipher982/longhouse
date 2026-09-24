---
name: zerg-ship
description: Zerg/Longhouse full ship cycle — test, deploy, QA, verify. Use when pushing changes to production or doing a full dev→deploy iteration.
---

# Zerg Ship Cycle

## Surfaces

- **Public demo runtime** — `https://longhouse.ai` — Coolify app `longhouse-demo`
- **Control plane** — `https://control.longhouse.ai` — private repo/service; public deploys only health-check it
- **Hosted tenant runtime** — `https://<subdomain>.longhouse.ai` — reprovisioned runtime container managed by the control plane

`longhouse.ai` is a demo-mode Longhouse runtime, not a static landing page.

## Ship Types

Do not blur these lanes:

- **Hosted deploy** — updates public Longhouse runtime surfaces running on the hosted runtime host:
  public demo runtime and hosted tenant runtimes. The hosted control plane is
  an external private service for this public repo; runtime deploys may check
  it, but do not ship it.
- **CLI/package release** — updates the user-installed `longhouse` CLI from
  the GitHub release wheel used by `scripts/install.sh`. Existing users do not
  get this from a hosted deploy. They need a new install or upgrade:
  `uv tool upgrade longhouse` or rerun the installer.
- **Runner release** — updates the separately installed runner binary/service.
  This is its own release/update path and is not covered by the normal runtime
  or control-plane deploy lanes.

If a change touches `longhouse claude`, local hook install, local launcher
behavior, `connect --install`, or other code that runs on the user's machine,
do not say "deployed" as if hosted users now have it. That kind of change
needs a CLI/package release, and sometimes users must rerun
`longhouse connect --install` after upgrading.

For launch readiness, use the exact-SHA verifier after the hosted and release
lanes have both run:

```bash
make launch-readiness SHA="<full-sha>"
```

This is stricter than hosted deploy success: it also checks latest release and
PyPI package build identity so the public installer cannot silently point at old
code.

## Background Wait Rule

If a tool or workflow already gives you a completion event or a blocking wait primitive, use it once and move on. Do not burn tool calls on `pgrep`, repeated curls, or ad hoc status polling loops while a background task is running.

Unchanged wait output is not a model decision seam. If the harness re-enters the
model whenever a blocking wait yields, do not generate repeated status turns.
Keep the durable run ID, report the pending operation once, and resume only when
the wait becomes terminal or new user input requires judgment.

Good:

```bash
gh run watch <id> --exit-status
make ship-watch SHA="<full-sha>"
```

Bad:

```bash
while pgrep -f playwright; do sleep 5; done
while true; do curl .../health; sleep 5; done
```

## Deploy Lanes

### Runtime lane

Changed paths typically include `server/**`, `web/**`, `engine/**`, `config/**`, `docker/runtime.dockerfile`.

What ships:
- GHCR runtime image tagged with the full source SHA (and `latest` only as a registry convenience)
- Public demo, canary, cohort, and explicit dogfood mutations through the durable
  private deployment API, using the exact immutable digest
- Canary smoke/functional acceptance before demo or any later ring; personal
  dogfood promotion is manual and names the selected verified release

Primary automation:

```bash
SHA="$(git rev-parse HEAD)"
make ship SHA="$SHA"
```

GitHub then runs at least:
- `runtime-image.yml`
- `deploy-and-verify.yml`

Other push workflows may also appear for the same SHA depending on touched paths.

For agent use, always prefer the explicit-SHA forms:

```bash
make ship SHA="<full-sha>"
make ship-watch SHA="<full-sha>"
```

This skill is the single source of truth for the repo's `cowbell` ship flow.

When the maintainer says `cowbell`, the agent owns the whole ship loop:

- resolve the task SHA yourself
- if the task is still uncommitted work, commit it now
- otherwise reuse the latest commit that represents the task you just finished, even if it was pushed earlier
- run `make ship SHA="<task-sha>"`
- read the start banner and confirm the exact target SHA + commit subject
- stay in the foreground until `make ship` exits
- do not wrap `make ship` in a short outer shell timeout; the monitor already has its own timeout
- cite exact SHAs, immutable image digests, deployment receipt IDs, and workflow run IDs

`deploy-and-verify.yml` waits for exact-SHA image publication before it submits
the canary candidate. The canary is functionally qualified before the public
demo submission. The fast smoke is the release completion signal; broad
`hosted-live-qa.yml` is dispatched asynchronously with the durable deployment
receipt and observes that receipt without owning or cancelling it. Full CI
remains an optional source gate (`DEPLOY_WAIT_FULL_CI=true`) rather than a
post-deploy sleep/poll loop. Every rapid-push outcome is explicit: deployed,
queued, superseded, rejected, or failed.

Before pushing, run `make affected-check BASE=<base-sha>` to see which CI
filters match the committed and local diff. Run focused tests for the changed
behavior, not `make test-ci` for every edit; a CSS change needs a web proof,
not backend or engine integration. The affected command does not replace
judgment about dependencies or the exact-SHA CI verdict. On `main`, use
`make ship SHA=<full-sha> ARGS=--json` for the single blocking verdict.
From a topic worktree, first rebase onto current `origin/main` and use
`make ship SHA=<full-sha> ARGS="--branch main --json"`; otherwise `ship`
pushes the topic branch and does not deploy. `make ship-watch SHA=<full-sha>
ARGS=--json` observes an already-pushed SHA. Do not babysit branch-latest CI.

If `make ship` returns non-zero for the target SHA, ship failed. You may explain why you think it failed, including suspected pre-existing drift, but do not relabel that outcome as success.

When releasing a held push through `workflow_dispatch`, dispatch
`runtime-image.yml` and `deploy-and-verify.yml` at the same exact SHA. Do not
use direct Coolify, SSH, Docker Compose, or host reprovision commands as a
deployment fallback; those bypass durable receipts and target fencing. If a
deployment is pending, use the deployment receipt/API observer or
`make ship-watch SHA="<full-sha>"`.

### Hosted Control Plane

The hosted control plane is no longer shipped from this public repo. Treat it
as an external service that runtime deploys depend on through
`CONTROL_PLANE_URL`.

Runtime deploys may still wait for `https://control.longhouse.ai/health` and
use the hosted instance helpers to reprovision canary instances. That is a
service dependency check, not a public control-plane source deploy.

If the control-plane service itself needs changes, switch to the private
control-plane repo and use its deploy instructions. Do not recreate
`control-plane/**` or `deploy-control-plane.yml` here.

### Mixed commits

If a commit touches both public runtime code and the external hosted control
plane, ship and verify each repository independently. Do not assume the public
runtime workflow deploys the control plane for you.

If a commit also changes local CLI/install behavior, that is a third concern:
hosted deploys may still be needed, but they do not replace publishing a new
CLI/package release.

## QA Harness

```bash
make qa-live
QA_INSTANCE_URL=https://other.longhouse.ai make qa-live
make qa-live-conversations
```

`qa-live` covers auth + timeline, forum redirect, session detail, health,
agents API, AI search toggle, recall, briefings, continuation readiness, and
auth refresh. It is now an async post-promote verifier for normal runtime
ships; failures still matter, but they should not block the fast deploy return
path.

Two auth systems:
- Browser pages: hosted login-token → `longhouse_session` cookie
- `/api/agents/*`: device token → `X-Agents-Token`

## Reprovision Hosted Tenant

Use the helper only as an API client: it resolves the explicitly selected
target and submits a durable deployment with the immutable digest. It does not
SSH to a host, run Compose, or mutate a container directly.

```bash
make reprovision SUBDOMAIN=other \
  IMAGE="ghcr.io/cipher982/longhouse-runtime@sha256:<verified-digest>"
```

Data survives deployment. Hosted tenant SQLite lives at
`/var/app-data/longhouse/<subdomain>/longhouse.db` on the runtime host and
`/data/longhouse.db` in the container; access those paths only for diagnosis,
not deployment mutation.

## Logs When Things Break

```bash
ssh <runtime-host> 'docker logs longhouse-<subdomain> --tail 50'
```

SSH and provider log commands are read-only diagnostics. They are not an
alternate release path; all canary, demo, cohort, and dogfood changes go
through the private deployment API.

## Local Dogfood Refresh (MANDATORY for machine-side changes)

**Hosted ship does NOT update the maintainer's laptop.** The `longhouse` CLI,
`longhouse-engine` daemon, and `Longhouse.app` menu bar are installed into his
system and only move when rebuilt locally. Automatic hosted promotion of this
personal dogfood surface is prohibited; any hosted dogfood promotion must name
the exact selected, canary-verified release.

After a successful `make ship`, run this when the task changes a locally
installed Longhouse component: CLI/package code, engine, Desktop App,
installer/connect/onboarding, hooks, or other machine-side behavior.

```bash
make dogfood-refresh
launchctl kickstart -k gui/$(id -u)/ai.longhouse.app
```

Skip this for `web/**`-only, docs-only, test-only, and hosted-runtime-only
changes; those do not alter the installed CLI, engine, or menu bar. For a
qualifying change, it rebuilds+reinstalls CLI/engine and restarts the menu bar
so it picks up the new `engine-status.json`.

**Shortcut:** for Python-CLI-only changes under `server/zerg/cli/`,
`cd server && uv tool install -e .` is ~5s vs ~60s. This is narrow —
does not apply to engine, hooks, connect, desktop app, or iOS.

### iOS

If the change touched `ios/`, the agent builds, verifies a rendered simulator
frame, then installs the finished build with `make phone-deploy` as the last
step. Do not claim iOS shipped from a hosted deploy or ask the maintainer to
build in Xcode; use the `zerg-ui` workflow and report a device-only blocker.

### End-of-ship prompt

Report the exact task SHA, runtime disposition (`deployed` or
`no_runtime_change`), the workflow/receipt IDs from the JSON verdict, and
what local refresh or iOS device installation was required. If there was no
runtime mutation, do not claim the demo or canary changed.

## Definition of Done

- [ ] Affected-change selection and focused behavioral proof passed before push
- [ ] Runtime/UI launch-surface changes had a matching client/fixture proof
- [ ] Correct deploy lane(s) used
- [ ] If local CLI/install behavior changed, a release/upgrade path was handled separately
- [ ] Public demo runtime healthy if runtime lane changed
- [ ] Control plane healthy if control-plane lane changed
- [ ] Hosted canary healthy if runtime lane changed
- [ ] Fast deploy smoke passed after hosted runtime changes
- [ ] Async hosted live QA was dispatched; if it fails, fix/re-run before release claims
- [ ] Local dogfood refreshed only if machine-side behavior changed
- [ ] iOS device installed only if `ios/` changed
