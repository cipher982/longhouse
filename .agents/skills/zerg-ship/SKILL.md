---
name: zerg-ship
description: Zerg/Longhouse full ship cycle — test, deploy, QA, verify. Use when pushing changes to production or doing a full dev→deploy iteration.
---

# Zerg Ship Cycle

## The Loop

```
make test            # unit tests (~9s, must pass)
git push origin main # triggers GHCR build if backend/frontend/dockerfile changed
gh run watch <id>    # wait for runtime image
~/git/me/mytech/scripts/coolify-deploy.sh longhouse-demo
~/git/me/mytech/scripts/coolify-deploy.sh longhouse-control-plane
# reprovision user instances (see below)
make qa-live         # 5 Playwright tests against live instance (~5s)
```

## QA Harness

```bash
make qa-live                          # default: david010.longhouse.ai
QA_INSTANCE_URL=https://other.longhouse.ai make qa-live  # other instance
./scripts/qa-live.sh --url https://...  # direct
```

Tests: auth + timeline, forum (with session rows), session detail, health, agents API.
Exit 0 = pass. Fail screenshots → `/tmp/qa-live-fail-{test}.png`.

Auth: reads `LONGHOUSE_PASSWORD` from env OR auto-fetches via `ssh zerg docker exec longhouse-david010`.
API calls use `~/.claude/longhouse-device-token` (`X-Agents-Token` header). Browser uses JWT cookie.

**Two auth systems — don't mix them:**
- Browser pages: password-login JWT → `longhouse_session` cookie
- `/api/agents/*` endpoints: device token → `X-Agents-Token` header

## Reprovision User Instance

```bash
# Find control plane container (hash changes on every deploy)
CONTAINER=$(ssh zerg "docker ps --format '{{.Names}}' | grep jkkcgcoo | head -1")
ADMIN_TOKEN=$(ssh zerg "docker exec $CONTAINER env | grep ADMIN_TOKEN | cut -d= -f2")

# Reprovision (stops+removes+recreates with latest image — data is safe, SQLite bind-mounted)
curl -s -X POST -H "X-Admin-Token: $ADMIN_TOKEN" https://control.longhouse.ai/api/instances/1/reprovision

# Verify health
sleep 15 && curl -s https://david010.longhouse.ai/api/health | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])"
```

Data survives reprovision — SQLite at `/var/lib/docker/data/longhouse/<subdomain>/longhouse.db` (host bind mount, not inside container).

## Wait for GHCR Build

```bash
# Find the build run
gh run list --limit 5 --json status,name,databaseId

# Wait for it
gh run watch <databaseId> --exit-status
```

Path filters: build only triggers if `apps/zerg/backend/`, `apps/zerg/frontend-web/`, or `docker/` changed. Docs-only pushes skip it.

## Architecture Quirks Agents Burn Time On

**Providers and the Python vs Rust shipper:**
- The Rust engine (`longhouse-engine`) does ALL actual session shipping. Python shipper is deleted.
- `longhouse connect --install` installs the Rust engine as a launchd/systemd service.
- Stop hook: `exec /abs/path/to/longhouse-engine ship --file "$TRANSCRIPT"` (path baked at install time).
- Presence hooks: UserPromptSubmit→thinking, PreToolUse→running, PostToolUse→thinking, Stop→idle.

**Presence system:**
- `POST /api/agents/presence` — upserted per session_id, stale after 10 min.
- Active sessions endpoint (`/api/agents/sessions/active`) joins presence table.
- Forum polls at 2s. Presence state drives entity glow on canvas.
- Hooks take effect on next session start — current session predates new hooks.

**DB is SQLite (not Postgres) for runtime instances:**
- `AgentsBase.metadata.create_all()` — no migrations, auto-created.
- WAL mode. `ix_events_session_id` and `ix_events_session_timestamp` indexes make event queries fast even at 900k+ rows.
- Never run `alembic` on agent models — no versions in `apps/zerg/backend/alembic/versions/`.

**Frontend generated types lag backend:**
- When adding fields to response models, update `src/services/api/agents.ts` manually — don't wait for openapi regen.
- TypeScript check: `bun run --cwd apps/zerg/frontend-web tsc --noEmit --skipLibCheck`.

**Canvas viewport snap (fixed but worth knowing):**
- ForumCanvas ResizeObserver must depend on `state.layout.grid.cols/rows` (primitives), NOT `state.layout` (object ref).
- Object ref changes on every 2s poll → re-centers viewport while user pans.

## Logs When Things Break

```bash
# User instance
ssh zerg 'docker logs longhouse-david010 --tail 50'

# Marketing site / control plane
coolify app logs longhouse-demo
coolify app logs longhouse-control-plane

# Engine daemon (on dev machine)
ls ~/.claude/logs/engine.log.*
tail -f ~/.claude/logs/engine.log.$(date +%Y-%m-%d)
```

## Definition of Done

- [ ] `make test` 96 passed
- [ ] `make qa-live` 5/5 passed
- [ ] `gh run watch` GHCR build success
- [ ] `curl -s https://david010.longhouse.ai/api/health | jq .status` → "healthy"
- [ ] Commit message references what shipped
