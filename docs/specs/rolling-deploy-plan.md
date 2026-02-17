# Rolling Deploy for Multi-Tenant Instances

**Status:** Final
**Author:** Claude (David's agent)
**Reviewed by:** Codex (GPT-5.2)
**Date:** 2026-02-16

## Problem

Today, updating user instances requires manual `curl` calls to reprovision each instance individually. With 1 instance this is fine; at 100+ it's untenable. There's no batch deploy, no health gating, no rollback, and no canary strategy. The control plane has no concept of "desired version" — it always pulls `:latest`.

## Current State

### What exists
- `POST /api/instances/{id}/reprovision` — stops container, pulls `:latest`, starts new container
- GHCR build tags images with both `:latest` and `:<7-char-SHA>` (via `docker/metadata-action`)
- Health check: `wait_for_health()` in provisioner polls `https://{subdomain}.longhouse.ai/api/health` for up to 120s
- `deploy-and-verify.yml` captures `previous_image_sha` but doesn't use it for rollback
- Instance model tracks `status` (provisioning/active/deprovisioned/failed) and `last_health_at`
- SQLite data lives on host bind mount — container replacement is data-safe

### What's missing
- No version tracking per instance (what image is running?)
- No batch reprovision endpoint
- No concurrency control (100 simultaneous pulls would crush the host)
- No automatic rollback on failure
- No canary/ring deployment
- No deploy history or audit trail

## Design

### 1. Deployment Table (Durable State)

A new `cp_deployments` table tracks each deploy as a first-class entity:

```python
class Deployment(Base):
    __tablename__ = "cp_deployments"

    id: Mapped[str]                    # e.g. "d-20260216-1430" (PK)
    image: Mapped[str]                 # Full image ref with tag
    image_digest: Mapped[str | None]   # Resolved sha256 digest (immutable)
    status: Mapped[str]                # pending | in_progress | completed | paused | failed
    rings: Mapped[str | None]          # JSON array of targeted rings, null = all
    max_parallel: Mapped[int]          # Concurrency limit
    failure_threshold: Mapped[int]     # Max failures before pause
    failure_count: Mapped[int] = 0
    reason: Mapped[str | None]         # Human-readable deploy reason
    created_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
```

This is the source of truth. The reconcile loop reads from it; crash recovery resumes from DB state.

### 2. Instance Model Changes

Add fields to the `Instance` model (`cp_instances` table):

```python
# Version tracking
current_image: str | None          # What's actually running (set after healthy provision)
desired_image: str | None          # What should be running (set by deploy-all)
last_healthy_image: str | None     # Last known good — updated only after health check passes
deploy_ring: int = 0               # 0 = canary (owner), 1 = early, 2 = general

# Deploy state
deploy_state: str = "idle"         # idle | pending | deploying | succeeded | failed | rolled_back
deploy_id: str | None              # FK to cp_deployments.id — which deploy is acting on this instance
deploy_started_at: datetime | None
deploy_error: str | None           # Last error message if failed
```

Key changes from draft (per Codex review):
- **`last_healthy_image`**: only updated after health check passes. Used as default rollback target. Solves the "current_image overwritten before rollback" problem.
- **`deploy_state` values refined**: `idle → pending → deploying → succeeded/failed/rolled_back`. No overlap with instance `status` (which tracks lifecycle: provisioning/active/deprovisioned).
- **`deploy_id`**: links instance to active deployment. Prevents races — reprovision checks this before proceeding.

Migration: additive nullable columns via `create_all()`.

### 3. Image Pinning in Provisioner

**Change `provision_instance()`** to accept an explicit `image` parameter:

```python
def provision_instance(self, subdomain, owner_email, password=None, image=None):
    image = image or settings.image  # fallback to :latest
    self.client.images.pull(image)
    # Resolve and store digest for immutable reference
    pulled = self.client.images.get(image)
    digest = pulled.attrs.get("RepoDigests", [None])[0]
    # ... create container with pinned image
    return ProvisionResult(..., image=image, image_digest=digest)
```

**After successful provision + health check**, update:
- `current_image = image`
- `last_healthy_image = image` (only on health success)

**On reprovision (single-instance)**: check `deploy_id` — if instance is part of an active deployment, return `409 Conflict` instead of proceeding. This prevents races between manual reprovision and batch deploy.

### 4. Batch Deploy API

**Create deployment:**
```
POST /api/deployments
X-Admin-Token: ...

{
  "image": "ghcr.io/cipher982/longhouse-runtime:a1b2c3d",  // required — explicit tag
  "max_parallel": 5,                                         // default 5
  "rings": [0],                                               // optional — which rings; omit for all
  "failure_threshold": 3,                                     // default 3
  "reason": "Deploy session timeline redesign",               // optional
  "dry_run": false                                            // optional
}
```

**Response (immediate):**
```json
{
  "id": "d-20260216-1430",
  "image": "ghcr.io/cipher982/longhouse-runtime:a1b2c3d",
  "image_digest": "sha256:abc123...",
  "status": "in_progress",
  "total_targeted": 42,
  "instances": [
    {"id": 1, "subdomain": "david010", "ring": 0, "deploy_state": "pending"}
  ]
}
```

`dry_run=true` returns the same response but with `status: "dry_run"` and does not modify any state. Shows exact instance list and skip reasons (deprovisioned, already on target image, etc.).

The endpoint:
1. Creates a `Deployment` record
2. Pre-pulls the image once (resolves digest)
3. Sets `desired_image` and `deploy_state=pending` and `deploy_id` on all targeted instances
4. Kicks off the background reconcile loop
5. Returns immediately

**Get deployment status:**
```
GET /api/deployments/{deploy_id}
X-Admin-Token: ...
```

**Response:**
```json
{
  "id": "d-20260216-1430",
  "image": "ghcr.io/cipher982/longhouse-runtime:a1b2c3d",
  "image_digest": "sha256:abc123...",
  "status": "in_progress",
  "total": 42,
  "succeeded": 35,
  "deploying": 5,
  "failed": 1,
  "pending": 1,
  "failure_threshold": 3,
  "created_at": "2026-02-16T14:30:00Z",
  "completed_at": null,
  "failed_instances": [
    {"id": 7, "subdomain": "alice", "deploy_error": "Health check timeout after 120s"}
  ]
}
```

**Rollback:**
```
POST /api/deployments/{deploy_id}/rollback
X-Admin-Token: ...

{
  "scope": "failed"  // "failed" (only failed instances) | "all" (everything in this deploy)
}
```

Rollback creates a **new Deployment** record targeting the `last_healthy_image` of each instance. This means rollback is auditable and uses the same reconcile loop.

### 5. Deploy Reconcile Loop

A background task that is **DB-driven and crash-recoverable**:

```python
async def run_deploy(deploy_id: str, db: Session):
    deploy = db.get(Deployment, deploy_id)
    if deploy.status not in ("pending", "in_progress"):
        return

    deploy.status = "in_progress"
    db.commit()

    # Pre-pull image once (already done at API level, but idempotent)
    provisioner.client.images.pull(deploy.image)

    # Get all instances for this deploy that haven't succeeded yet
    instances = db.query(Instance).filter(
        Instance.deploy_id == deploy_id,
        Instance.deploy_state.in_(["pending", "failed"]),  # retry failed on resume
    ).order_by(Instance.deploy_ring).all()

    for batch in chunked(instances, deploy.max_parallel):
        # Check failure threshold BEFORE starting batch
        if deploy.failure_count >= deploy.failure_threshold:
            deploy.status = "paused"
            for inst in remaining_instances:
                inst.deploy_state = "idle"
                inst.deploy_id = None
            db.commit()
            return

        results = await asyncio.gather(*[
            deploy_single_instance(inst, deploy, db)
            for inst in batch
        ])

        for success, inst in results:
            if not success:
                deploy.failure_count += 1
        db.commit()

    deploy.status = "completed" if deploy.failure_count == 0 else "failed"
    deploy.completed_at = utcnow()
    db.commit()


async def deploy_single_instance(inst, deploy, db):
    inst.deploy_state = "deploying"
    inst.deploy_started_at = utcnow()
    db.commit()

    try:
        provisioner.deprovision_instance(inst.container_name)
        result = provisioner.provision_instance(
            inst.subdomain, inst.owner_email, image=deploy.image
        )
        inst.container_name = result.container_name
        healthy = await provisioner.wait_for_health(inst.subdomain, timeout=120)

        if healthy:
            inst.deploy_state = "succeeded"
            inst.current_image = deploy.image
            inst.last_healthy_image = deploy.image
            inst.status = "active"
            inst.last_health_at = utcnow()
            return (True, inst)
        else:
            raise Exception("Health check timeout after 120s")

    except Exception as e:
        inst.deploy_state = "failed"
        inst.deploy_error = str(e)[:500]
        # Attempt rollback to last known good
        if inst.last_healthy_image and inst.last_healthy_image != deploy.image:
            try:
                provisioner.deprovision_instance(inst.container_name)
                result = provisioner.provision_instance(
                    inst.subdomain, inst.owner_email, image=inst.last_healthy_image
                )
                inst.container_name = result.container_name
                inst.deploy_state = "rolled_back"
                inst.current_image = inst.last_healthy_image
            except Exception:
                pass  # Double failure — instance is down, needs manual intervention
        return (False, inst)
    finally:
        db.commit()
```

**Crash recovery**: on control plane startup, check for any Deployment with `status=in_progress`. Resume the reconcile loop — it picks up instances still in `pending` or `failed` state. Instances stuck in `deploying` for >5 minutes are marked `failed` and retried.

### 6. Ring-Based Canary Strategy

| Ring | Who | When |
|------|-----|------|
| 0 | David's instance (`david010`) | First — manual verification |
| 1 | Test/ephemeral instances | After ring 0 verified |
| 2 | All remaining instances | After ring 1 healthy |

**Ring prerequisites enforced**: deploying to ring N requires all instances in rings < N to have `deploy_state=succeeded` for the same image, unless `force=true` is passed. Returns `409` with details otherwise.

Typical deploy sequence:

```bash
# Step 1: Canary
curl -X POST .../deployments -d '{"image": "...:<sha>", "rings": [0]}'
# Manually verify david010.longhouse.ai

# Step 2: Early ring
curl -X POST .../deployments -d '{"image": "...:<sha>", "rings": [1]}'

# Step 3: General availability
curl -X POST .../deployments -d '{"image": "...:<sha>", "rings": [2]}'
```

### 7. Concurrency Guards

- **Per-instance**: `deploy_id` on Instance acts as a soft lock. `POST /api/instances/{id}/reprovision` checks `deploy_id` — if set and deployment is active, returns `409 Conflict`.
- **Global**: `POST /api/deployments` checks for any deployment with `status=in_progress`. Only one active deployment at a time. Returns `409` if one exists.
- **Stale state cleanup**: on startup, instances with `deploy_state=deploying` and `deploy_started_at` older than 5 minutes are marked `failed`.

### 8. Backfill Existing Instances

Before the first batch deploy, a one-time admin endpoint populates `current_image` from running containers:

```
POST /api/instances/backfill-images
X-Admin-Token: ...
```

For each active instance, inspects the running Docker container and sets `current_image` and `last_healthy_image` from the container's image reference.

### 9. CI/CD Integration

Update `deploy-and-verify.yml` to pass the SHA tag:

```yaml
- name: Deploy canary
  run: |
    IMAGE="ghcr.io/cipher982/longhouse-runtime:${{ steps.meta.outputs.version }}"
    curl -X POST \
      -H "X-Admin-Token: ${{ secrets.ADMIN_TOKEN }}" \
      -H "Content-Type: application/json" \
      -d "{\"image\": \"$IMAGE\", \"rings\": [0], \"reason\": \"CI deploy ${{ github.sha }}\"}" \
      https://control.longhouse.ai/api/deployments
```

Full ring automation (0 → 1 → 2 with health gates) is a future CI enhancement.

## Implementation Phases

### Phase 1: Foundation (implement now)
- Instance model changes (new columns)
- `Deployment` table
- Image pinning in provisioner (accept `image` param, resolve digest, store on instance)
- Backfill endpoint
- Concurrency guards on single-instance reprovision

### Phase 2: Batch Deploy
- `POST /api/deployments` endpoint
- `GET /api/deployments/{id}` endpoint
- Deployer service with reconcile loop
- Crash recovery on startup
- Failure threshold + pause logic

### Phase 3: Rollback & Rings
- `POST /api/deployments/{id}/rollback` endpoint
- Ring prerequisite enforcement
- Per-instance auto-rollback on failure (in reconcile loop)

### Phase 4: CI Integration (later)
- Update `deploy-and-verify.yml`
- Automated ring progression

## Files to Modify

| File | Changes |
|------|---------|
| `apps/control-plane/control_plane/models.py` | Add `Deployment` model, add version/deploy fields to `Instance` |
| `apps/control-plane/control_plane/services/provisioner.py` | Accept `image` param, resolve digest, return image info |
| `apps/control-plane/control_plane/routers/instances.py` | Concurrency guard on reprovision, backfill endpoint |
| `apps/control-plane/control_plane/routers/deployments.py` | **New** — create, status, rollback endpoints |
| `apps/control-plane/control_plane/services/deployer.py` | **New** — reconcile loop, batch logic, crash recovery |
| `apps/control-plane/control_plane/config.py` | Add `deploy_max_parallel`, `deploy_failure_threshold` |
| `apps/control-plane/control_plane/main.py` | Startup hook for crash recovery (resume stale deploys) |

## What Stays the Same

- Single-instance reprovision — unchanged API, gains concurrency guard
- Provisioner's Docker API calls (pull, create, start, stop, rm)
- Caddy label routing
- Instance auth, SSO, secrets injection
- SQLite bind mount data persistence
- Stripe billing/webhook flow

## Constraints & Decisions

1. **SQLite single-writer** — can't run old+new simultaneously. Brief downtime (seconds) per instance is acceptable. Mitigated by pre-pulling image before stopping container.
2. **No queue system** — asyncio background tasks + DB-driven state. Single control plane process; at 100 instances with max_parallel=5, this is sufficient.
3. **Failure threshold** — default 3, checked before each batch starts (not after). Prevents cascading failures.
4. **Image digest stored** — resolved at pull time for immutable reference. Tag kept for readability. Rollback uses `last_healthy_image` which includes the tag.
5. **One active deployment** — simplicity over parallelism. No overlapping deploys.
6. **Ring assignment** — manual via admin API. Auto-assignment is future work.

## Verification

1. Unit tests for deployer service (mock Docker client, test batch/failure/crash-recovery)
2. `dry_run=true` → verify targeting logic and skip reasons
3. Deploy ring 0 (david010) → verify health check passes, `current_image` updated
4. Deploy ring 2 → verify batch concurrency and failure threshold pauses correctly
5. Kill control plane mid-deploy → restart → verify reconcile resumes from DB state
6. Deploy bad image → verify per-instance rollback to `last_healthy_image` + global pause
7. `deploy-status` shows accurate counts and failed instance details throughout

## Resolved Questions (from Codex review)

1. **Crash recovery**: Deployment + instance state is fully DB-driven. Startup hook resumes incomplete deploys.
2. **Rollback target**: `last_healthy_image` (only set after health check passes) is the default. Explicit image override available.
3. **Race conditions**: `deploy_id` on instance + global single-active-deployment constraint prevent concurrent modification.
4. **Audit trail**: `Deployment` table serves as deploy history. Per-instance `deploy_error` captures failure details.
5. **Stale state**: 5-minute TTL on `deploying` state; marked `failed` on startup.
6. **Backfill**: One-time `POST /api/instances/backfill-images` reads running container image refs.
