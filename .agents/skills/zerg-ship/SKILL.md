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

## Background Wait Rule

If a tool or workflow already gives you a completion event or a blocking wait primitive, use it once and move on. Do not burn tool calls on `pgrep`, repeated curls, or ad hoc status polling loops while a background task is running.

Good:

```bash
gh run watch <id> --exit-status
./scripts/ops/coolify-deploy.sh longhouse-demo --timeout 900
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
- GHCR runtime image tagged as `latest` plus the full commit SHA
- Public demo runtime via Coolify using that shared image
- Hosted canary tenant via reprovision

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
- do not wrap `make ship` in a short outer shell timeout; the monitor already has its own timeout and successful runtime ships commonly take around 5 to 8 minutes, sometimes longer if they wait behind earlier `main` deploys
- cite exact SHAs and workflow run ids when reporting status

`deploy-and-verify.yml` waits for the matching `contract-first-ci.yml` and `runtime-image.yml` runs for the same SHA before any remote deploy action. Manual dispatch stays isolated for recovery use.

If `make ship` returns non-zero for the target SHA, ship failed. You may explain why you think it failed, including suspected pre-existing drift, but do not relabel that outcome as success.

Manual fallback:

```bash
./scripts/ops/coolify-deploy.sh longhouse-demo \
  --docker-image ghcr.io/cipher982/longhouse-runtime \
  --docker-tag <full-commit-sha> \
  --timeout 900
make reprovision IMAGE="ghcr.io/cipher982/longhouse-runtime:<full-commit-sha>"
make qa-live
make qa-live-conversations
```

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

`qa-live` covers auth + timeline, forum redirect, session detail, health, agents API, AI search toggle, recall, briefings, continuation readiness, and auth refresh.

Two auth systems:
- Browser pages: hosted login-token → `longhouse_session` cookie
- `/api/agents/*`: device token → `X-Agents-Token`

## Reprovision Hosted Tenant

```bash
make reprovision
make reprovision SUBDOMAIN=other
```

Data survives reprovision. Hosted tenant SQLite lives at `/var/app-data/longhouse/<subdomain>/longhouse.db` on the host and `/data/longhouse.db` in the container.

## Logs When Things Break

```bash
ssh <runtime-host> 'docker logs longhouse-<subdomain> --tail 50'
coolify app logs longhouse-demo
coolify app logs longhouse-control-plane
```

## Local Dogfood Refresh (MANDATORY after every ship)

**Hosted ship does NOT update the maintainer's laptop.** The `longhouse` CLI,
`longhouse-engine` daemon, and `Longhouse.app` menu bar are installed
into his system and only move when rebuilt locally. If you forget this
step, the menu bar will show "restart pending" and the maintainer is stuck
dogfooding old code.

After **every** successful `make ship` — not conditionally, not "if
runtime changed," always — run:

```bash
make dogfood-refresh
launchctl kickstart -k gui/$(id -u)/ai.longhouse.app
```

That rebuilds+reinstalls CLI/engine and restarts the menu bar so it
picks up the new `engine-status.json`. Takes ~1 minute.

**Shortcut:** for Python-CLI-only changes under `server/zerg/cli/`,
`cd server && uv tool install -e .` is ~5s vs ~60s. This is narrow —
does not apply to engine, hooks, connect, desktop app, or iOS.

### iOS

If the change touched `ios/`, tell the maintainer explicitly at the end of
the ship: iOS has no TestFlight/App Store path yet. He has to plug
his phone in via USB and build via Xcode. Do not claim "shipped"
for iOS changes without calling this out.

### End-of-ship prompt

Always end a successful ship by reporting:
- exact SHA now live on demo + canary
- confirmation that `make dogfood-refresh` ran (or why you skipped it)
- whether iOS needs a manual Xcode rebuild

## Definition of Done

- [ ] `make test-ci` passed before push
- [ ] `make test-e2e` passed before push when UI/runtime changed
- [ ] Correct deploy lane(s) used
- [ ] If local CLI/install behavior changed, a release/upgrade path was handled separately
- [ ] Public demo runtime healthy if runtime lane changed
- [ ] Control plane healthy if control-plane lane changed
- [ ] Hosted canary healthy if runtime lane changed
- [ ] `make qa-live` passed after hosted runtime changes
- [ ] `make dogfood-refresh` ran + menu bar restarted (always)
- [ ] iOS rebuild prompt given if `ios/` changed
