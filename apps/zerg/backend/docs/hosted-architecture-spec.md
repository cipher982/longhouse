# Longhouse Hosted Architecture Spec

Version: 2026-02-04

## Purpose

Define the complete hosted architecture: control plane, instance provisioning, jobs, and the retirement of standalone Sauron. One provisioning path for all users (including David).

---

## Principles

- **One provisioning path** — Control plane provisions ALL instances, no exceptions.
- **Instance isolation** — Each user gets their own container with SQLite DB.
- **Jobs are built-in** — Every instance has an always-on scheduler (see jobs-system-spec.md).
- **No standalone Sauron** — Sauron functionality is absorbed into instances.
- **Control plane is dumb** — It provisions containers and manages billing. It does NOT run user workloads.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  CONTROL PLANE (Coolify-managed, one instance)                  │
│  - Google OAuth for signup                                      │
│  - Stripe billing                                               │
│  - Docker API provisioning                                      │
│  - Instance health monitoring                                   │
└─────────────────────────────────────────────────────────────────┘
        │
        │ provisions via Docker API
        ▼
┌─────────────────────────────────────────────────────────────────┐
│  USER INSTANCES (control-plane provisioned)                     │
│                                                                 │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  │
│  │ david.lh.ai     │  │ alice.lh.ai     │  │ bob.lh.ai       │  │
│  │ ┌─────────────┐ │  │ ┌─────────────┐ │  │ ┌─────────────┐ │  │
│  │ │ Frontend    │ │  │ │ Frontend    │ │  │ │ Frontend    │ │  │
│  │ │ Backend     │ │  │ │ Backend     │ │  │ │ Backend     │ │  │
│  │ │ Scheduler   │ │  │ │ Scheduler   │ │  │ │ Scheduler   │ │  │
│  │ │ Jobs Repo   │ │  │ │ Jobs Repo   │ │  │ │ Jobs Repo   │ │  │
│  │ │ SQLite DB   │ │  │ │ SQLite DB   │ │  │ │ SQLite DB   │ │  │
│  │ └─────────────┘ │  │ └─────────────┘ │  │ └─────────────┘ │  │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Control Plane

**What it does:**
- Signup (Google OAuth)
- Billing (Stripe checkout, webhooks, portal)
- Provisioning (Docker API → create container with Caddy labels)
- Health monitoring (poll `/api/health`)
- Instance management (start, stop, deprovision)

**What it does NOT do:**
- Run user workloads
- Store user data (only billing/account metadata)
- Execute jobs

**Deployment:**
- Coolify-managed app on zerg
- Postgres DB for control plane metadata
- Docker socket access for provisioning

**Endpoints:**
| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Control plane health |
| `POST /auth/google` | OAuth signup/login |
| `POST /checkout` | Create Stripe checkout |
| `POST /webhooks/stripe` | Handle payment events |
| `GET /instances/{subdomain}` | Instance status |
| `POST /instances/{subdomain}/provision` | Provision (internal/admin) |
| `DELETE /instances/{subdomain}` | Deprovision |

---

## Instance Provisioning

**Trigger:** Stripe `invoice.paid` webhook (or admin API).

**Process:**
1. Control plane receives payment confirmation
2. Generate subdomain from user email or preference
3. Call Docker API to create container:
   ```
   docker run -d \
     --name longhouse-{subdomain} \
     --label caddy={subdomain}.longhouse.ai \
     --label "caddy.reverse_proxy={{upstreams 8000}}" \
     -v /var/lib/docker/data/longhouse/{subdomain}:/data \
     -e INSTANCE_ID={subdomain} \
     -e OWNER_EMAIL={email} \
     -e SINGLE_TENANT=1 \
     -e DATABASE_URL=sqlite:////data/longhouse.db \
     ... (secrets) \
     ghcr.io/cipher982/longhouse-runtime:latest
   ```
4. Connect container to `coolify` network
5. Wait for `/api/health` to return 200
6. Redirect user to `{subdomain}.longhouse.ai`

**Volume structure:**
```
/var/lib/docker/data/longhouse/{subdomain}/
  longhouse.db       # SQLite database
  jobs/              # Jobs repo (auto-created)
    manifest.py
    jobs/
    .git/
```

---

## Runtime Image

**Dockerfile:** `docker/runtime.dockerfile`

**Contents:**
- Python backend (FastAPI)
- Frontend (React, served via StaticFiles)
- All dependencies bundled

**Key behaviors:**
- Serves frontend at `/`
- Serves API at `/api/*`
- Scheduler starts automatically
- Jobs repo bootstrapped on first boot

---

## Jobs (Per-Instance)

See `jobs-system-spec.md` for full details.

**Summary:**
- Every instance has `/data/jobs/` repo
- Scheduler is always-on (no env toggle)
- Jobs defined in `manifest.py`
- Commis can write/edit jobs
- Optional GitHub sync via UI

---

## Sauron Retirement

**Current state:**
- Standalone service on Clifford
- Pulls jobs from `sauron-jobs` repo
- Runs David's personal automation

**Target state:**
- Sauron service is shut down
- David's instance (`david.longhouse.ai`) runs his jobs
- `sauron-jobs` repo becomes David's instance jobs repo

**Migration steps:**
1. Ensure David's instance has jobs enabled (always-on by default)
2. Configure GitHub sync to `sauron-jobs` repo via UI
3. Verify jobs run in instance
4. Shut down `sauron-scheduler` on Clifford
5. Archive or delete `apps/sauron/` from repo

---

## Coolify Apps (Target State)

| App | Server | Purpose |
|-----|--------|---------|
| `control-plane` | zerg | Provisions instances |
| ~~`sauron-scheduler`~~ | ~~Clifford~~ | RETIRED |
| ~~`zerg-api`~~ | ~~zerg~~ | RETIRED (replaced by instances) |
| ~~`zerg-web`~~ | ~~zerg~~ | RETIRED (replaced by instances) |

**After migration:**
- Only `control-plane` on Coolify
- All user instances (including David's) are Docker containers provisioned by control plane

---

## DNS & Routing

**Wildcard DNS:** `*.longhouse.ai` → zerg IP (Cloudflare proxied) ✅ DONE

**Routing:** Coolify's Caddy proxy (caddy-docker-proxy) routes by container labels.

**TLS:** Cloudflare handles SSL termination (proxied mode).

---

## Auth Flow (Instance Access)

**Option A: Password auth (v1)**
- Instance has `LONGHOUSE_PASSWORD` env var
- User enters password → session cookie

**Option B: Control plane bridge (v2)**
1. User logs into control plane (Google OAuth)
2. Control plane issues short-lived JWT
3. Redirect to `{subdomain}.longhouse.ai?token=xxx`
4. Instance validates JWT, sets session cookie

**v1 ships first.** Password auth is already implemented.

---

## Implementation Phases

### Phase 1: Control Plane on Coolify
- [ ] Create Coolify app for control plane
- [ ] Migrate env vars from manual docker-compose
- [ ] Verify provisioning still works
- [ ] Shut down manual `~/longhouse-control-plane/` deployment

### Phase 2: Runtime Image
- [ ] Push `longhouse-runtime` to ghcr.io
- [ ] Update control plane to use runtime image
- [ ] Test full provisioning flow with real subdomain

### Phase 3: Migrate David's Instance
- [ ] Provision `david.longhouse.ai` via control plane
- [ ] Configure jobs GitHub sync to `sauron-jobs`
- [ ] Verify jobs run correctly
- [ ] Shut down `zerg-api` and `zerg-web` on Coolify

### Phase 4: Retire Sauron
- [ ] Verify David's jobs running in instance
- [ ] Shut down `sauron-scheduler` on Clifford
- [ ] Archive `apps/sauron/` (or delete)

### Phase 5: Stripe Integration
- [ ] Implement checkout flow
- [ ] Implement webhooks
- [ ] Test payment → provision flow

---

## Open Questions

1. **Instance naming:** subdomain from email (`alice` from `alice@example.com`) or user-chosen?
2. **Trial flow:** provision immediately with time limit, or require payment first?
3. **Data retention:** how long to keep volume after subscription cancels?
4. **Backup strategy:** Litestream to S3, or periodic SQLite dump?

---

## Acceptance Criteria

- [ ] Control plane provisions instances via single code path
- [ ] David's instance is provisioned same way as any user
- [ ] Jobs run inside instances (no standalone Sauron)
- [ ] `zerg-api`, `zerg-web`, `sauron-scheduler` all retired
- [ ] Only `control-plane` remains on Coolify
