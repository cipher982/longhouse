# Longhouse Jobs: Core Spec (0→1)

Version: 2026-02-04

## Purpose
Make scheduled jobs a **core, always-on** feature of every Longhouse instance.
Jobs must be simple for OSS onboarding yet powerful for power users.
No hidden toggles, no subtle fallbacks, no separate services.

---

## Principles (Hard Rules)
- **One code path** for jobs in all instances (hosted + OSS).
- **Always-on scheduler** inside the standard Longhouse service.
- **Jobs live as code files** (not JSON blobs).
- **Local versioning by default** (git init + auto-commit).
- **Remote sync is optional**, configured via UI (not env vars).
- **Fail loudly** on misconfiguration (no silent skips).

---

## Mental Model

Per-user Longhouse instance contains:
- Web UI + API
- Scheduler (APScheduler) + durable queue
- Jobs repo on disk

Sauron is **not a separate service**. It is the in-process jobs subsystem.

---

## Storage Model

Jobs live in a repo inside the instance volume:
```
/data/jobs/
  manifest.py
  jobs/
    daily_digest.py
    cleanup.py
  .git/
```

**Default behavior**
- On first boot, Longhouse creates `/data/jobs`, writes `manifest.py`, and runs `git init`.
- Every job change triggers an auto-commit.
- Remote sync is optional and configured via UI.

---

## Scheduler Behavior

**Always on in runtime** (no user-facing env toggles).
- Scheduler is started during normal app startup.
- Only skipped in tests (explicit `testing` guard).

---

## Job Loading

**Source of truth:** `/data/jobs/manifest.py`

Rules:
- The manifest must exist (auto-created on first boot).
- The loader assumes the manifest exists; failure to read is an error.
- External git sync is **not** required for core functionality.

Example manifest pattern:
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

---

## Git Versioning (Local First)

Longhouse manages a local git repo:
- `git init` if missing
- `git add -A && git commit -m "Update jobs: ..."` on every change

No remote required.

---

## Optional GitHub Sync (UI-driven)

Goal: backup jobs without forcing repo setup.

**Hosted: OAuth App (best UX)**
- "Connect GitHub" -> OAuth -> repo picker -> sync

**OSS: PAT fallback**
- "Connect GitHub" -> paste token + repo -> verify -> sync

Remote config stored in DB (encrypted), not in env.

Sync behaviors:
- Manual "Sync Now" button
- Optional auto-push on every commit

---

## UX Surface (v1)

### Jobs Page
- List jobs (id, cron, enabled, description)
- Run now / Enable / Disable
- Open jobs repo in Commis workspace

### Jobs Repo Panel
- Status: repo ready / remote connected / last sync time
- Buttons:
  - Initialize Jobs Repo (first boot only)
  - Connect GitHub
  - Sync Now

---

## Commis Authoring (Core Workflow)

The commis workspace can write to `/data/jobs`:
- Create new job files in `jobs/`
- Update `manifest.py`
- Run manual job test
- Auto-commit + optional sync

This matches the "open Claude Code, write job, deploy" experience.

---

## API Surface (Minimal)

Existing:
- `GET /api/jobs`
- `POST /api/jobs/{id}/run`
- `POST /api/jobs/{id}/enable`
- `POST /api/jobs/{id}/disable`

New (repo management):
- `GET /api/jobs/repo`
- `POST /api/jobs/repo/init`
- `POST /api/jobs/repo/verify`
- `POST /api/jobs/repo/sync`

---

## Implementation Steps (0→1)

1) **Repo bootstrap**
   - Ensure `/data/jobs` exists
   - Write starter `manifest.py` if missing
   - `git init`

2) **Make scheduler always-on**
   - Remove `JOB_QUEUE_ENABLED` gate for runtime
   - Keep testing-only guard

3) **Local manifest loader**
   - Load `/data/jobs/manifest.py` directly
   - Fail loudly if unreadable

4) **Auto-commit on change**
   - Add lightweight repo manager service
   - Commit after job changes

5) **UI repo panel + sync**
   - Show status
   - Configure remote
   - Verify + sync

6) **Optional: OAuth App**
   - Hosted only for smooth UX

---

## Failure Handling (No Silent Fallbacks)

- If manifest is missing and cannot be created -> fail startup with clear error.
- If git init fails -> fail with clear error.
- If sync fails -> surface error to user (do not disable jobs).

---

## Security

- GitHub tokens stored encrypted in instance DB.
- Repo credentials never logged.

---

## Acceptance Criteria

- Every instance has a jobs repo created on first boot.
- Scheduler runs jobs without any env toggles.
- Jobs are editable by commis in `/data/jobs`.
- Local git history exists by default.
- Optional GitHub sync works via UI.
