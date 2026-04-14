---
name: zerg-ship
description: Zerg/Longhouse full ship cycle — test, deploy, QA, verify. Use when pushing changes to production or doing a full dev→deploy iteration.
---

# Zerg Ship Cycle

## Surfaces

- **Public demo runtime** — `https://longhouse.ai` — Coolify app `longhouse-demo`
- **Control plane** — `https://control.longhouse.ai` — Coolify app `longhouse-control-plane`
- **Hosted tenant runtime** — `https://david010.longhouse.ai` — reprovisioned runtime container managed by the control plane

`longhouse.ai` is a demo-mode Longhouse runtime, not a static landing page.

## Ship Types

Do not blur these lanes:

- **Hosted deploy** — updates Longhouse surfaces running on `zerg`:
  public demo runtime, control plane, hosted tenant runtimes.
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

Use them even if the commit was pushed earlier in the session. `make ship SHA=<full-sha>` prints a start banner with the exact target SHA and commit subject so wrong-commit mistakes are obvious immediately. Do not use branch-latest `gh run list --limit 1` patterns on a busy `main`, and do not infer delayed `cowbell` requests from current `HEAD`. `deploy-and-verify.yml` does wait for the matching `contract-first-ci.yml` and `runtime-image.yml` runs for the same SHA before any remote deploy action, and manual dispatch stays isolated for recovery use.

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

### Control-plane lane

Changed paths typically include `control-plane/**`.

What ships:
- Control plane only

Primary automation:
- `deploy-control-plane.yml`

The control-plane lane waits for the matching `contract-first-ci.yml` run for the same SHA before any remote deploy step. Anchor all checks on the pushed SHA; do not infer state from the latest branch run.

Manual fallback:

```bash
./scripts/ops/coolify-deploy.sh longhouse-control-plane --timeout 900
./scripts/qa/smoke-prod.sh --no-llm
./scripts/ops/check-cp-credentials.sh
```

### Mixed commits

If a commit touches both runtime and control-plane lanes, expect both workflows to matter. Do not assume the runtime workflow deploys the control plane for you.

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
ssh zerg 'docker logs longhouse-david010 --tail 50'
coolify app logs longhouse-demo
coolify app logs longhouse-control-plane
```

## Definition of Done

- [ ] `make test-ci` passed before push
- [ ] `make test-e2e` passed before push when UI/runtime changed
- [ ] Correct deploy lane(s) used
- [ ] If local CLI/install behavior changed, a release/upgrade path was handled separately
- [ ] Public demo runtime healthy if runtime lane changed
- [ ] Control plane healthy if control-plane lane changed
- [ ] Hosted canary healthy if runtime lane changed
- [ ] `make qa-live` passed after hosted runtime changes
