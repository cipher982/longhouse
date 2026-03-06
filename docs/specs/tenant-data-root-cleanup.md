# Tenant Data Root Cleanup

Status: Done (2026-03-06)

## Executive Summary

This sprint finishes the hosted-instance storage cleanup that started with moving live tenant data off `/` on `zerg`.

The sprint started with two architectural leaks:

1. Control-plane code and docs treated `/var/lib/docker/data/longhouse` as canonical.
2. Repairing malformed tenant SQLite GUID values required manual sqlite surgery.

This sprint does four things:

1. Add a one-shot tenant DB admin tool that scans for malformed GUID values and repairs the safe nullable columns automatically.
2. Make `/var/app-data/longhouse` the canonical hosted instance data root in code and config.
3. Migrate persisted `cp_instances.data_path` values to the new root and remove the host compatibility bind mount.
4. Use the cleanup momentum to delete three more small pieces of drift or duplication.

## Non-Goals

- No orchestrator rewrite.
- No Coolify storage-tab surgery unless a hard blocker appears.
- No broad GUID rewrite of primary keys or foreign-key graphs.
- No enterprise fallback matrix; the hosted path should stay opinionated and simple.

## Decision Log

### Decision: Keep host-path preparation, but only against the canonical mounted root
Context: `_volume_for()` and `run_migration_preflight()` already create/chown/chmod the target instance directory. Removing that behavior entirely would require a second ownership-initialization path for new instances.
Choice: Keep the host-path preparation logic, centralize it in one helper, and mount `/var/app-data/longhouse` directly into the control-plane container.
Rationale: This keeps provisioning simple and robust without relying on the old compatibility bind mount.
Revisit if: A future provisioner path can initialize ownership without any direct host-path mount.

### Decision: Existing instance records should be migrated, not supported forever
Context: `cp_instances.data_path` currently stores old-path values like `/var/lib/docker/data/longhouse/david010`.
Choice: Rewrite those rows in a one-shot migration to `/var/app-data/longhouse/<subdomain>`.
Rationale: Carrying both path shapes forever would keep the system confusing and preserve dead fallback code.
Revisit if: Another host still uses the old layout and cannot be migrated in the same rollout.

### Decision: The GUID repair tool will only auto-fix safe nullable columns
Context: Some GUID columns are IDs/FKs where blind rewriting could corrupt data; others are observability or assistant-message linkage fields where nulling/regenerating is safe.
Choice: Auto-fix only the safe nullable columns and fail/report on unsupported malformed key columns.
Rationale: This solves the real operational problem without pretending a generic GUID rewrites tool is safe.
Revisit if: We find malformed GUIDs in key columns that need a dedicated migration.

### Decision: Change the Coolify app definition once, then remove the compatibility bind mount
Context: Coolify storage editing is brittle, but the control-plane app only had one relevant mount to change. Leaving the old definition in place would keep future reprovisions coupled to the compatibility bind mount.
Choice: Update the control-plane Coolify storage + env to `/var/app-data/longhouse`, reprovision active instances onto the new path, then remove the host compatibility bind mount.
Rationale: One explicit app-definition change is simpler than inventing a second provisioning mechanism or carrying dead mounts forever.
Revisit if: Coolify exposes a safer first-class storage API and this process can be codified further.

## Design

### Canonical Hosted Data Root

Canonical host path:

```text
/var/app-data/longhouse/<subdomain>/
```

Hosted runtime mount remains:

```text
host path -> /data
```

The runtime still uses:

```text
DATABASE_URL=sqlite:////data/longhouse.db
```

Only the host-side source path changes.

### Provisioner Contract

Provisioner should accept an explicit host data path and use that exact string when binding `/data`.

Desired shape:

- `build_instance_data_path(subdomain)` returns `settings.instance_data_root/subdomain`
- `provision_instance(..., data_path=None)` uses `data_path` if provided, otherwise the canonical helper
- `run_migration_preflight(..., data_path=None)` uses the same helper/path selection
- No host-path `os.makedirs/chown/chmod` dependency inside the control-plane container

### Tenant GUID Repair Tool

One-shot script scope:

- Scans tenant DBs under a provided root or a single DB path.
- Reports malformed GUID values by table/column/row.
- Repairs only safe nullable columns in-place.
- Defaults to dry-run; `--apply` mutates.

Initial safe-repair targets:

- `runs.assistant_message_id` -> `NULL`
- `runs.trace_id` -> `NULL`
- Additional nullable observability/correlation GUID columns only if they are clearly non-key and safe.

Unsafe malformed key columns should be reported and exit non-zero rather than guessed.

## Implementation Phases

### Phase 1: Spec + Admin Tool

Acceptance criteria:

- Spec committed.
- Tenant DB admin tool exists with dry-run and apply modes.
- Tool can scan a root of instance directories and a single DB path.
- Tool has tests for malformed `runs.assistant_message_id` repair.

### Phase 2: Canonical Data Root In Code

Acceptance criteria:

- Control-plane default `instance_data_root` is `/var/app-data/longhouse`.
- Provisioner host-path preparation is centralized in one helper and points at the mounted canonical root.
- Reprovision path uses persisted `inst.data_path` when present.
- Host-ops tooling defaults use `/var/app-data/longhouse`.
- Repo docs/scripts stop presenting the old root path as canonical.

### Phase 3: Live Migration

Acceptance criteria:

- `cp_instances.data_path` rows on `zerg` point at `/var/app-data/longhouse/<subdomain>`.
- Host compatibility bind mount `/var/app-data/longhouse -> /var/lib/docker/data/longhouse` is removed.
- Active hosted instances reprovision successfully after the cutover.

### Phase 4: Verification

Acceptance criteria:

- `make test` passes.
- `make test-e2e` passes.
- Runtime image workflow succeeds.
- Hosted apps deploy/reprovision cleanly.
- `make qa-live` passes.
- Targeted live voice SSE E2E passes.

### Phase 5: Additional Simplification Pass

Acceptance criteria:

- Three more small simplifications land after the main cleanup.
- Each one removes drift, duplicate code, or obsolete compatibility behavior.
- The final summary includes what was deleted/simplified and why.

## Verification Commands

```bash
make test
make test-e2e
gh run watch <runtime-image-run-id> --exit-status
make qa-live
CONTROL_PLANE_ADMIN_TOKEN=... INSTANCE_SUBDOMAIN=david010 ./scripts/run-prod-e2e.sh tests/live/live_voice_sse.spec.ts
```
