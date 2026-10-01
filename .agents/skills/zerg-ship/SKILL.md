---
name: zerg-ship
description: Zerg/Longhouse full ship cycle — test, deploy, QA, verify. Use when pushing changes to production or doing a full dev→deploy iteration.
---

# Zerg Ship Cycle

The workspace AGENTS.md owns the ring table, the landing flow (worktree,
`make validate`, the review rule) and the agent-authority table. This skill owns
the mechanics: which command moves which surface, with which arguments, and how
to read the result.

## Surfaces

- **Public demo runtime** — `https://longhouse.ai` — operator-managed Docker Compose app
- **Control plane** — `https://control.longhouse.ai` — private repo/service; public deploys only health-check it
- **Hosted tenant runtime** — `https://<subdomain>.longhouse.ai` — reprovisioned runtime container managed by the control plane

`longhouse.ai` is a demo-mode Longhouse runtime, not a static landing page.

## Ship Types

Do not blur these lanes:

- **Hosted deploy** — a `main` push touching runtime paths publishes the
  runtime image and qualifies it on the canary automatically. Dogfood and
  production are separate rings that move only by command
  (`control-plane/docs/specs/release-rings.md`):
  - `make promote-dogfood SHA=<full-sha>`: the SHA is required (the script
    refuses an empty one) and needs a canary-qualified Deploy and Verify run
    on it. The instance comes from `SUBDOMAIN` or `LONGHOUSE_DEFAULT_SUBDOMAIN`.
  - `make promote-production [SHA=<full-sha>] [CHECK=1]`: moves every other
    hosted tenant, the new-tenant default and the public demo to the exact
    image dogfood serves. `SHA` is the full 40-character commit and optional
    (omitted, it means what dogfood serves); `VERSION=` is refused. Four gates
    must pass: dogfood serves that digest, a passed (never superseded) Hosted
    Live QA run of main's workflow file on that SHA, the previous-release
    engine smoke, and the control plane's soak (none until the first real
    tenant exists, then 24 h; an unreadable answer refuses). `CHECK=1` prints
    the receipt and moves nothing. After a halted tenant wave, rerun with
    `PROMOTION_ATTEMPT=<n+1>`.
  - Both refuse a range holding non-exempt commits without a completed review
    receipt or with an unresolved blocking/material finding
    (`scripts/ops/review_gate.py`, `scripts/ops/review-policy.toml`).
  - No tag or laptop release is involved. The control plane itself is an
    external private service, not shipped here.
- **CLI/package release** (`make release VERSION=vX.Y.Z`; see
  [RELEASE.md](../../../RELEASE.md)) — publishes the native pair (`longhouse`,
  `longhouse-engine`), `Longhouse.app` and the PyPI wheel. Existing users do not
  get this from a hosted deploy: they rerun the installer (`scripts/install.sh`)
  or `uv tool upgrade longhouse` for the Python package. It is independent of
  production promotion and runs only from a clean `main` equal to `origin/main`
  (the primary checkout). Validation (~7 min) holds the heavy-build lock alone;
  rerunning the same `VERSION` resumes and skips validation for an
  already-validated candidate (`RELEASE_REVALIDATE=1` forces it).
- **iOS TestFlight** — `make testflight [SHA=<sha>] [WHATS_NEW="tester note"]`
  dispatches `ios-testflight.yml` (archive on a hosted macOS runner with the
  iOS 26 SDK, upload, wait for processing, Beta App Review submission when
  needed, publish to the public group) and waits for it. The SHA defaults to
  `HEAD` and must already be on `origin/main`. `make testflight-status` shows
  builds, review state and the public link and needs the `ASC_API_*` credentials
  in the environment. The public link is `https://testflight.apple.com/join/CmEd5kY7`;
  publishing fails when the group's link is not the one the landing page
  advertises (`web/src/features/marketing/landing/links.ts`). Hosted deploys,
  laptop releases and `make phone-deploy` never ship to TestFlight.
- **Runner release** — updates the separately installed runner binary/service.
  This is its own release/update path and is not covered by the normal runtime
  or control-plane deploy lanes.

If a change touches `longhouse claude`, local hook install, local launcher
behavior, `longhouse machine repair`, or other code that runs on the user's
machine, do not say "deployed" as if hosted users now have it. That kind of
change needs a CLI/package release, and sometimes users must run
`longhouse machine repair` after upgrading.

For launch readiness, use the exact-SHA verifier after the hosted and release
lanes have both run:

```bash
make launch-readiness SHA="<full-sha>"
```

This is stricter than hosted deploy success: it also checks latest release and
PyPI package build identity so the public installer cannot silently point at old
code. By default it also requires the public demo to serve that SHA, which only
happens after `make promote-production`; `ARGS=--skip-demo` skips just that
check (`release.sh` always passes it).

## Land It

Before pushing, run `make affected-check BASE=<base-sha>` to see which CI
filters match the committed and local diff, run focused tests for the changed
behavior (not `make test-ci` for every edit; a CSS change needs a web proof, not
backend or engine integration), then `make check-push-readiness` (~1 s). It
reports stale duplicate commits on `main` (roll forward, never force-push),
applies the review landing rule, and checks that the verifier/subject import
allowlist only shrinks (`scripts/ci/verifier_boundary.py`).

The landing rule (text in the workspace AGENTS.md): a commit touching the
blocking list in `scripts/ops/review-policy.toml` needs a completed review
receipt first. `make check-push-readiness`, `make ship`, `release.sh` and a
pre-push hook all ask the same gate. Run `make install-push-gate` once per clone
so a bare `git push origin HEAD:main` is asked too; the hook blocks only a real
refusal and lets a gate crash through with the reason printed. Only the owner
overrides it.

## Background Wait Rule

If a tool or workflow already gives you a completion event or a blocking wait primitive, use it once and move on. Do not burn tool calls on `pgrep`, repeated curls, or ad hoc status polling loops while a background task is running.

Unchanged wait output is not a model decision seam. If the harness re-enters the
model whenever a blocking wait yields, do not generate repeated status turns.
Keep the durable run ID, report the pending operation once, and resume only when
the wait becomes terminal or new user input requires judgment.

For a queued GitHub run, keep API traffic bounded. `gh run watch` defaults
to a three-second interval (1,200 reads/hour per watcher); simultaneous
long-running watches hit GitHub's 403 rate limit. Use:

```bash
gh run watch <id> --interval 60 --compact --exit-status
make ship-watch SHA="<full-sha>" ARGS="--poll 30"
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
- Canary mutations through the durable private deployment API, using the exact
  immutable digest
- Canary smoke/functional acceptance. Dogfood and production promotion are
  separate rings (above) and both name the selected verified release

Primary automation:

```bash
SHA="$(git rev-parse HEAD)"
make ship SHA="$SHA"
```

`make ship` (`scripts/ops/ship.sh`) pushes that exact SHA (after the review and
verifier-boundary checks), prints how busy CI is, then waits on the push-triggered
runs for that SHA. GitHub runs at least:
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

`deploy-and-verify.yml` waits for exact-SHA image publication, reprovisions
the hosted canary, and qualifies it with a fast smoke. Its "Deploy public demo
runtime" job verifies the image and prints promotion instructions; it does
**not** mutate the demo container. The demo moves on the production ring
(`make promote-production`, after dogfood serves the SHA), not on every
push, so `make ship`/`ship-monitor` do not require it to match the pushed
SHA — a stale demo SHA there is expected, not `live_drift`; it is still
printed in `make deploy-status`/`ship-watch` output. Hosted Live QA is
dispatched asynchronously (see Hosted QA) and must be checked before final QA.
The canary is shared: a run whose canary a newer push replaced mid-run ends
green as *superseded* (step "Stand down, canary superseded", receipts in its
summary), which is not a verdict on your SHA. Check the newest commit's run.
Full CI remains an optional source gate (`DEPLOY_WAIT_FULL_CI=true`).

On `main`, use `make ship SHA=<full-sha> ARGS=--json` for the single blocking
verdict. From a topic worktree, first rebase onto current `origin/main` and use
`make ship SHA=<full-sha> ARGS="--branch main --json"`; otherwise `ship`
pushes the topic branch and does not deploy. `make ship-watch SHA=<full-sha>
ARGS=--json` observes an already-pushed SHA. Do not babysit branch-latest CI.

If `make ship` returns non-zero for the target SHA, ship failed. You may explain why you think it failed, including suspected pre-existing drift, but do not relabel that outcome as success.

When releasing a held push through `workflow_dispatch`, dispatch
`runtime-image.yml` and `deploy-and-verify.yml` at the same exact SHA.
Never use direct SSH/Compose as an alternate hosted-tenant deploy: it bypasses
the deployment receipt and target fencing. The demo's own Compose stack is
the exception because it is not a control-plane tenant.

### Public demo promotion

The demo is on the production ring, not the per-push hosted-deploy lane: it
moves only via `make promote-production` (arguments and gates above), which
pins the demo's Compose stack to the same qualified digest and verifies it.
A refusal names every gate that failed. If the tenant wave halts or the demo
pin fails, the script prints the recovery commands and keeps its receipt under
`/tmp/agents/promotion-receipts/`. The public demo is an operator-managed
Compose stack, **not** a control-plane tenant — `promote-production.sh` updates
its durable image pin over SSH and recreates only the demo service, preserving
its data. Never register the demo as a hosted tenant or use its Compose
mechanism to bypass hosted-tenant deployment receipts. Verify the live surface:

```bash
make deploy-status
curl -fsS https://longhouse.ai/api/readyz
```

A successful exact-SHA `make ship-watch` does not imply the demo moved; it is
only deployed once `make promote-production` reports `demo_verified=true` for
that SHA.

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

## Hosted QA

Hosted Live QA (`hosted-live-qa.yml`) is the post-deploy verifier. Deploy and
Verify dispatches it asynchronously for each qualified runtime SHA, and it also
runs daily. It deploys the exact commit to its own dedicated instance (a push
never redeploys it), runs `qa-live` (authenticated timeline, owned
transcript/search, closed-state projection and launch-picker smoke, plus the
provider-binding audit) through the portable test boundary, and records a verdict
per SHA. Read that verdict to answer "did QA pass this SHA"; a passed,
non-superseded verdict is a production-promotion gate. The workspace AGENTS.md
Rings table has the command to run it for any commit with a published image.

`make qa-live` is not a developer-machine command: the isolation dispatcher
refuses it without `--live --image ... --credentials`, it needs
`QA_INSTANCE_SUBDOMAIN` naming a dedicated canary plus `SMOKE_RUNTIME_TOKEN`,
and `scripts/qa/qa-live.sh` refuses personal, dogfood and demo targets.

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

Data survives deployment. A hosted tenant keeps its databases under
`/var/app-data/longhouse/<subdomain>/` on the runtime host (`/data/` in the
container); access them only for diagnosis, not deployment mutation
(`zerg-hosted-debug`).

## Logs When Things Break

```bash
ssh <runtime-host> 'docker logs longhouse-<subdomain> --tail 50'
```

SSH/container log commands are read-only diagnostics for hosted tenants.
Canary, cohort, and dogfood mutations use the private deployment API; the
public demo moves only via `make promote-production`, which pins its own
Compose stack over SSH rather than a hand-run command.

## Local Dogfood Refresh (MANDATORY for machine-side changes)

**Hosted ship does NOT update the maintainer's laptop.** The `longhouse` CLI,
`longhouse-engine` daemon, and `Longhouse.app` menu bar are installed into his
system and only move when rebuilt locally. Automatic hosted promotion of this
personal dogfood surface is prohibited; any hosted dogfood promotion must name
the exact selected, canary-verified release.

After a successful `make ship`, run this when the task changes a locally
installed Longhouse component: CLI/package code, engine, Desktop App,
installer/onboarding, hooks, or other machine-side behavior.

```bash
make dogfood-refresh
launchctl kickstart -k gui/$(id -u)/ai.longhouse.app
```

Skip this for `web/**`-only, docs-only, test-only, and hosted-runtime-only
changes; those do not alter the installed CLI, engine, or menu bar. For a
qualifying change, it rebuilds+reinstalls CLI/engine and restarts the menu bar
so it picks up the new `engine-status.json`. Afterwards confirm the machine
still ships (`longhouse-server sessions list --limit 1` shows a fresh last
activity).

**Shortcut:** for Python-CLI-only changes under `server/zerg/cli/`,
`cd server && uv tool install -e .` is ~5s vs ~60s. This is narrow —
does not apply to engine, hooks, onboarding, desktop app, or iOS.

### iOS

If the change touched `ios/`, the agent builds, verifies a rendered simulator
frame, then installs the finished build with `make phone-deploy` as the last
step. Do not claim iOS shipped from a hosted deploy or ask the maintainer to build in Xcode; use the
`zerg-ui` workflow and report a device-only blocker. Testers get iOS only
through TestFlight (above).

### End-of-ship prompt

Report the exact task SHA, runtime disposition (`deployed` or
`no_runtime_change`), the workflow/receipt IDs from the JSON verdict, and
what local refresh or iOS device installation was required. If there was no
runtime mutation, do not claim the demo or canary changed.

## Definition of Done

- [ ] Affected-change selection and focused behavioral proof passed before push
- [ ] `make check-push-readiness` passed (review receipt present for blocking-list paths)
- [ ] Runtime/UI launch-surface changes had a matching client/fixture proof
- [ ] Correct deploy lane(s) used
- [ ] If local CLI/install behavior changed, a release/upgrade path was handled separately
- [ ] Hosted canary healthy and serving the exact SHA if the runtime lane changed
- [ ] Fast deploy smoke passed after hosted runtime changes
- [ ] Hosted Live QA verdict for the SHA is `passed`, or it is named as pending/failed in the report
- [ ] Demo and production claimed only after `make promote-production` reported `demo_verified=true`
- [ ] Control plane healthy if control-plane lane changed
- [ ] Local dogfood refreshed only if machine-side behavior changed
- [ ] iOS device installed only if `ios/` changed
