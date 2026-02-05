# SDP-1: Hosted Platform Spec

**Status:** Mostly Complete
**Version:** 2026-02-05
**Owner:** david010@gmail.com

## Current State (2026-02-05)

- [x] Phase 1: Control plane on Coolify proxy (control.longhouse.ai working)
- [x] Phase 2: Runtime image (ghcr.io/cipher982/longhouse-runtime:latest)
- [x] Phase 3: Jobs system (local manifest loading, /api/jobs/repo endpoints)
- [x] Phase 4: David's instance (david.longhouse.ai provisioned)
- [ ] Phase 5: Retire Sauron (sauron-scheduler still on Clifford)

**Remaining work:**
- Debug sauron-jobs manifest import errors (lib dependencies missing in runtime)
- Shut down sauron-scheduler on Clifford once jobs verified working
- Clean up old zerg-api/zerg-web on Coolify

## Executive Summary

Transform Longhouse from split Coolify deployments to unified control-plane-provisioned instances with built-in jobs. Retire standalone Sauron. One provisioning path for all users (including David).

---

## Principles

1. **One provisioning path** — Control plane provisions ALL instances, no exceptions.
2. **Instance isolation** — Each user gets their own container with SQLite DB.
3. **Jobs are built-in** — Every instance has an always-on scheduler.
4. **No standalone Sauron** — Sauron functionality is absorbed into instances.
5. **Control plane is dumb** — It provisions containers and manages billing. It does NOT run user workloads.

---

## Architecture

```
                         CONTROL PLANE
                    (Coolify-managed on zerg)
                    control.longhouse.ai
    ┌──────────────────────────────────────────────┐
    │  - Google OAuth signup                       │
    │  - Stripe billing                            │
    │  - Docker API provisioning                   │
    │  - Instance health monitoring                │
    └──────────────────────────────────────────────┘
                         │
                         │ Docker API
                         ▼
    ┌──────────────────────────────────────────────┐
    │  USER INSTANCES (Docker containers on zerg)  │
    │                                              │
    │  ┌────────────────┐  ┌────────────────┐      │
    │  │ david.lh.ai    │  │ alice.lh.ai    │ ...  │
    │  │                │  │                │      │
    │  │ Frontend       │  │ Frontend       │      │
    │  │ Backend        │  │ Backend        │      │
    │  │ Scheduler      │  │ Scheduler      │      │
    │  │ Jobs Repo      │  │ Jobs Repo      │      │
    │  │ SQLite DB      │  │ SQLite DB      │      │
    │  └────────────────┘  └────────────────┘      │
    └──────────────────────────────────────────────┘
                         │
    Routing: Caddy (coolify-proxy) with caddy-docker-proxy labels
    DNS: *.longhouse.ai → zerg IP (Cloudflare proxied)
```

---

## Components

### Control Plane (`apps/control-plane/`)

**Responsibilities:**
- Signup (Google OAuth)
- Billing (Stripe checkout, webhooks, portal)
- Provisioning (Docker API → create container with Caddy labels)
- Health monitoring (poll `/api/health`)
- Instance management (start, stop, deprovision)

**Does NOT:**
- Run user workloads
- Store user data (only billing/account metadata)
- Execute jobs

**Deployment:** Coolify-managed app at `control.longhouse.ai`

**Key Endpoints:**
| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Control plane health |
| `POST /auth/google` | OAuth signup/login |
| `POST /checkout` | Create Stripe checkout |
| `POST /webhooks/stripe` | Handle payment events |
| `GET /admin/instances/{subdomain}` | Instance status |
| `POST /admin/instances` | Provision instance |
| `DELETE /admin/instances/{subdomain}` | Deprovision instance |

### Runtime Image (`docker/runtime.dockerfile`)

Single container serving frontend + backend:
- Python backend (FastAPI) at `/api/*`
- React frontend (static) at `/`
- Scheduler starts automatically
- Jobs repo bootstrapped on first boot

**Image:** `ghcr.io/cipher982/longhouse-runtime:latest`

### Per-Instance Volume Structure

```
/var/lib/docker/data/longhouse/{subdomain}/
  longhouse.db       # SQLite database
  jobs/              # Jobs repo (auto-created)
    manifest.py      # Job definitions
    jobs/            # Job modules
    .git/            # Local version control
```

---

## Jobs System

### Principles

1. **Always-on scheduler** — No `JOB_QUEUE_ENABLED` toggle in production.
2. **Jobs live as code** — Python files in `/data/jobs/`, not JSON blobs.
3. **Local versioning by default** — `git init` + auto-commit on changes.
4. **Remote sync optional** — GitHub sync configured via UI, not env vars.
5. **Fail loudly** — No silent fallbacks on misconfiguration.

### Bootstrap Flow (First Boot)

1. Check `/data/jobs/` exists
2. If missing, create directory structure
3. Write starter `manifest.py` template
4. Run `git init`

### Manifest Format

```python
from zerg.jobs import job_registry, JobConfig
from jobs.daily_digest import run as daily_digest

job_registry.register(JobConfig(
    id="daily-digest",
    cron="0 8 * * *",
    func=daily_digest,
    description="Send my daily summary.",
))
```

### API Surface

| Endpoint | Purpose |
|----------|---------|
| `GET /api/jobs` | List registered jobs |
| `POST /api/jobs/{id}/run` | Run job immediately |
| `POST /api/jobs/{id}/enable` | Enable job |
| `POST /api/jobs/{id}/disable` | Disable job |
| `GET /api/jobs/repo` | Repo status (initialized, remote connected, etc.) |
| `POST /api/jobs/repo/init` | Initialize jobs repo |
| `POST /api/jobs/repo/sync` | Sync with remote |

---

## DNS & Routing

**Wildcard DNS:** `*.longhouse.ai` → zerg IP (Cloudflare proxied)
**Control plane:** `control.longhouse.ai` (A record)
**Routing:** Caddy (existing coolify-proxy) via caddy-docker-proxy labels
**TLS:** Cloudflare handles SSL termination (proxied mode)

---

## Auth Flow

**v1 (shipping first):** Password auth
- Instance has `LONGHOUSE_PASSWORD` env var
- User enters password → session cookie

**v2 (future):** Control plane bridge
1. User logs into control plane (Google OAuth)
2. Control plane issues short-lived JWT
3. Redirect to `{subdomain}.longhouse.ai?token=xxx`
4. Instance validates JWT, sets session cookie

---

## Migration Path

### Current State
- `zerg-api` and `zerg-web` on Coolify (shared deployment)
- `sauron-scheduler` on Clifford (standalone)
- Manual `~/longhouse-control-plane/` docker-compose on zerg

### Target State
- Control plane on Coolify at `control.longhouse.ai`
- All user instances (including David's) provisioned by control plane
- No `zerg-api`, `zerg-web`, or `sauron-scheduler`

### David's Migration
1. Pre-populate `/var/lib/docker/data/longhouse/david/jobs/` with `sauron-jobs` clone
2. Provision `david` instance via control plane
3. Verify jobs run on schedule
4. Shut down old Coolify apps + Sauron

---

## Implementation Phases

### Phase 1: Control Plane to Coolify
Move control plane from manual docker-compose to Coolify.

- [ ] Add `control.longhouse.ai` A record in Cloudflare → zerg IP (proxied)
- [ ] Create Coolify app for control plane (source: `apps/control-plane/`)
- [ ] Configure Docker socket mount in Coolify
- [ ] Migrate env vars from `~/longhouse-control-plane/control-plane.env`
- [ ] Test provisioning via Coolify-managed control plane
- [ ] Shut down `~/longhouse-control-plane/` containers

**Acceptance:**
- `curl https://control.longhouse.ai/health` returns ok
- Admin can provision test instance via Coolify-managed control plane

### Phase 2: Runtime Image
Build and deploy unified frontend+backend image.

- [ ] Build runtime image from `docker/runtime.dockerfile`
- [ ] Push to ghcr.io as `longhouse-runtime:latest`
- [ ] Update control plane `CONTROL_PLANE_IMAGE` env var
- [ ] Provision test instance with runtime image
- [ ] Verify frontend + API + scheduler all work

**Acceptance:**
- `curl https://testuser.longhouse.ai/` returns React app
- `curl https://testuser.longhouse.ai/api/health` returns ok
- Scheduler starts (check container logs)

### Phase 3: Jobs System (Always-On)
Make scheduler always-on, implement jobs repo bootstrap.

- [ ] Remove `JOB_QUEUE_ENABLED` gate from startup
- [ ] Implement `/data/jobs/` bootstrap on first boot
- [ ] Auto-create `manifest.py` template
- [ ] Auto-run `git init` in jobs dir
- [ ] Add `/api/jobs/repo` endpoints

**Files:**
- `zerg/cli/main.py` (serve command)
- `zerg/main.py` (lifespan)
- `zerg/services/jobs_repo.py` (new)
- `zerg/routers/jobs.py`

**Acceptance:**
- Fresh instance creates `/data/jobs/` with manifest
- Jobs load from manifest without env config
- `GET /api/jobs/repo` returns repo status

### Phase 4: Migrate David's Instance
Provision david.longhouse.ai via control plane.

- [ ] Pre-populate `/var/lib/docker/data/longhouse/david/jobs/` with `sauron-jobs` clone
- [ ] Provision `david` instance via control plane
- [ ] Verify jobs load from pre-populated repo
- [ ] Verify jobs run on schedule
- [ ] Shut down `zerg-api` and `zerg-web` on Coolify

**Acceptance:**
- `david.longhouse.ai` is accessible
- Jobs from `sauron-jobs` run on schedule
- Old Coolify apps are stopped

### Phase 5: Retire Sauron
Shut down standalone Sauron service.

- [ ] Verify all David's jobs running in instance
- [ ] Shut down `sauron-scheduler` on Clifford
- [ ] Archive or delete `apps/sauron/`

**Acceptance:**
- No Sauron container running on Clifford
- Jobs continue running in david.longhouse.ai

### Phase 6: Stripe Integration
Not in scope for initial run. Separate task.

---

## Verification

After each phase:
1. Run verification against acceptance criteria
2. Commit with `[SDP-1] hosted-platform` tag

End-to-end test:
```bash
# Provision fresh instance
curl -X POST https://control.longhouse.ai/admin/instances \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"subdomain": "e2etest", "owner_email": "test@example.com"}'

# Wait for health
sleep 30
curl https://e2etest.longhouse.ai/api/health

# Check jobs repo exists
curl https://e2etest.longhouse.ai/api/jobs/repo

# Clean up
curl -X DELETE https://control.longhouse.ai/admin/instances/e2etest
```

---

## Decisions

1. **Data migration:** Fresh start for David. No export/import from current zerg-api.
2. **Jobs config:** Pre-populate volume. Clone `sauron-jobs` into `/data/jobs/` before first boot.
3. **Control plane URL:** `control.longhouse.ai` (dedicated subdomain)
4. **Infrastructure:** Control plane + instances all on zerg (single host for now).
5. **Proxy:** Use existing Coolify Caddy proxy (caddy-docker-proxy).

---

## Open Questions

1. **Instance naming:** subdomain from email (`alice` from `alice@example.com`) or user-chosen?
2. **Trial flow:** provision immediately with time limit, or require payment first?
3. **Data retention:** how long to keep volume after subscription cancels?
4. **Backup strategy:** Litestream to S3, or periodic SQLite dump?

---

## Consolidated From

- `apps/zerg/backend/docs/hosted-architecture-spec.md`
- `apps/zerg/backend/docs/jobs-system-spec.md`
