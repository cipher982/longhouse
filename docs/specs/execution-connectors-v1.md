# Runners v1: Cloud Brain + Edge Executor

**Status:** Draft (implementable)
**Owner:** Zerg backend
**Primary goal:** Replace `ssh_exec` with `runner_exec` as the production execution primitive, without moving the worker/LLM loop onto user machines.

---

## Summary

Zerg already has the right abstraction:

- **Supervisor** delegates to **Workers**
- **Workers** do the iterative LLM loop and call tools

This spec introduces a new execution tool:

- `runner_exec(target, command, timeout_secs=...)`

`runner_exec` routes the command to a user-owned **Runner** (a lightweight daemon) over an outbound-only connection, runs it _near the target environment_, and returns stdout/stderr + exit code to the worker.

This keeps the “brain” (LLM + tool choice) in the SaaS and moves the “hands” (command execution) to user-controlled infrastructure.

---

## Goals

- **Multi-tenant correct-by-construction:** no backend access to user SSH keys by default.
- **Works behind NAT/private networks:** outbound-only runner connection.
- **Drop-in for workers:** mirrors the ergonomics of `ssh_exec` (single command, timeout, returns output).
- **Auditable:** every exec is recorded with `(owner_id, worker_id, runner_id, command, exit_code, duration_ms)`.
- **Safe default posture:** runners start in "read-only" capability mode (tight allowlist).

## Non-goals (v1)

- Running the full worker/LLM loop on user machines ("edge brain").
- Full file transfer / interactive PTY sessions.
- A complete policy language (we will start with simple allowlists and explicit opt-in).
- Cross-org "server inventory import" as an execution mechanism (can be layered on later by running SSH _from_ a runner).

---

## Terminology

**Decision: Use "Runner" naming** to avoid collision with existing OAuth connectors.

This repo uses "connectors" for third-party OAuth integrations (Slack/Gmail/etc). "Agent" collides with the core domain model (Supervisor/Worker agents). "Runner" is clean, intuitive, and follows industry precedent (GitHub Actions runners, GitLab runners).

For this feature:

- **Runner**: a user-run daemon that connects outbound to Swarmlet and executes jobs.
- **Runner Job**: a single execution request (command + timeout + optional metadata).

Naming convention:

- UI: "Add Runner", "Runners" section
- Code/DB: `runners`, `runner_jobs`
- Tool: `runner_exec`
- API: `/api/runners/*`

---

## Architecture

```
User -> Supervisor (SaaS) -> spawn_worker(task)
                      Worker (SaaS LLM loop)
                          |
                          | runner_exec(target="clifford", command="df -h")
                          v
                 Control plane routes to runner
                          v
              Runner (user-owned machine) runs command locally
                          |
                          v
                  stdout/stderr/exit_code -> Worker -> Supervisor -> User
```

Important: The **worker chooses commands** (as it does today). The runner is "dumb-ish": execute, stream, return.

---

## Onboarding UX (v1)

### Recommended onboarding (fast + realistic)

"Install a runner on a machine that already has access to your world":

- Laptop (has Tailscale, `~/.ssh/config`, etc)
- Bastion/jump host
- A single "ops box" inside their private network

This avoids "install on every prod server" while still letting users reach many targets (including SSHing onward if they prefer).

### UI flow

1. User clicks **Add Runner**
2. Backend returns:
   - `SWARMLET_URL`
   - `ENROLL_TOKEN` (one-time, TTL e.g. 10 minutes)
   - `docker run ...` (copy/paste)
3. Runner registers and appears "Online".

### Runner runtime packaging

- Default: Docker image `swarmlet-runner`
- Optional later: single binary + systemd unit

---

## Data model

### `runners`

- `id` (int)
- `owner_id` (int, FK users.id)
- `name` (string, user-editable, unique per owner) — used for target resolution
- `labels` (JSON) — e.g. `{ "role": "laptop", "env": "prod", "region": "us-east" }`
- `capabilities` (JSON array) — e.g. `["exec.readonly"]`, `["exec.full", "docker"]`
- `status` (string enum) — `online|offline|revoked`
- `last_seen_at` (datetime)
- `created_at`, `updated_at`
- `auth_secret_hash` (string) — store only a hash of the long-lived secret
- `metadata` (JSON) — hostname/os/arch/version/docker_available

### `runner_enroll_tokens`

- `id` (int)
- `owner_id` (int)
- `token_hash` (string)
- `expires_at` (datetime)
- `used_at` (datetime nullable)
- `created_at`

### `runner_jobs` (audit + optional queue)

- `id` (uuid)
- `owner_id` (int)
- `worker_id` (string nullable) — link to `WorkerArtifactStore`
- `run_id` (string nullable)
- `runner_id` (int)
- `command` (text)
- `timeout_secs` (int)
- `status` — `queued|running|success|failed|timeout|canceled`
- `exit_code` (int nullable)
- `started_at`, `finished_at`
- `stdout_trunc`, `stderr_trunc` (text) — capped/truncated
- `error` (text nullable)
- `artifacts` (JSON nullable) — reserved for future file upload support

Note: For v1, jobs can be "direct RPC" over the websocket without durable queuing, but the audit row should still be written. The `artifacts` field is NULL for v1 but reserves schema space for future `[{"name": "...", "size": ..., "url": "..."}]` support.

---

## API surface

### Enrollment

- `POST /api/runners/enroll-token`
  - auth required
  - returns `{ enroll_token, expires_at, swarmlet_url, docker_command }`

- `POST /api/runners/register`
  - body: `{ enroll_token, metadata, name?, labels? }`
  - returns: `{ runner_id, runner_secret }`
  - enrollment token becomes used/invalid immediately

### Management

- `GET /api/runners`
- `PATCH /api/runners/{id}` (rename/labels/capabilities)
- `POST /api/runners/{id}/rotate-secret`
- `POST /api/runners/{id}/revoke`

---

## Runner transport

Use WebSocket (fits the existing `/api/ws` infra; keep it separate from UI topics):

- `GET /api/runners/ws` (runner connects here)
- Auth via first message: `hello { runner_id, runner_secret }`

Backend responsibilities:

- Mark runner online/offline with heartbeats
- Maintain a routing map: `(owner_id, runner_id) -> websocket connection`
- Enforce that runners only receive jobs for their `owner_id`

---

## WebSocket protocol (minimal v1)

All messages are JSON.

### Runner -> server

- `hello`
  - `{ "type": "hello", "runner_id": 123, "secret": "...", "metadata": {...} }`
  - `metadata` should include `docker_available: bool` if runner can access Docker
- `heartbeat`
  - `{ "type": "heartbeat" }`
- `exec_chunk`
  - `{ "type": "exec_chunk", "job_id": "...", "stream": "stdout|stderr", "data": "..." }`
- `exec_done`
  - `{ "type": "exec_done", "job_id": "...", "exit_code": 0, "duration_ms": 1234 }`
- `exec_error`
  - `{ "type": "exec_error", "job_id": "...", "error": "..." }`

### Server -> runner

- `exec_request`
  - `{ "type": "exec_request", "job_id": "...", "command": "df -h", "timeout_secs": 30 }`
- `exec_cancel` (optional v1)
  - `{ "type": "exec_cancel", "job_id": "..." }`

Constraints:

- Output must be size-capped per job (e.g. 10–50KB combined) with truncation.
- Concurrency cap per runner (default 1 running job at a time for v1).

---

## Tool contract (what workers call)

Add a new built-in tool:

### `runner_exec(target: str, command: str, timeout_secs: int = 30) -> dict`

**Decision: Support both ID and name-based targeting from day one.**

- `target` can be:
  - a runner id (`"runner:123"`) — explicit, unambiguous
  - or a runner name (`"laptop"`, `"clifford-ops"`) — resolved per-user for ergonomics

Name-based targeting is preferred for UX. Workers naturally say "run this on clifford" rather than "run this on runner:8". Resolution is simple: `SELECT id FROM runners WHERE owner_id = ? AND name = ?`.

```python
def resolve_target(owner_id: int, target: str) -> int | None:
    if target.startswith("runner:"):
        return int(target.split(":")[1])
    # Name lookup (unique per owner)
    return db.query(Runner).filter_by(owner_id=owner_id, name=target).one_or_none()?.id
```

Return envelope should match the style of `ssh_exec`:

```json
{
  "ok": true,
  "data": {
    "target": "clifford-ops",
    "command": "df -h",
    "exit_code": 0,
    "stdout": "...",
    "stderr": "",
    "duration_ms": 1234
  }
}
```

Error envelope for:

- no runner with that name/id found
- runner offline
- policy denial
- timeout
- runner disconnected mid-job

---

## Permissions & safety (v1)

### Capability modes

Each runner has a capability set, enforced **server-side** (routing) and **runner-side** (execution gate).

Start with:

- `exec.readonly` (default)
  - allowlist only (see below)
- `exec.full` (explicit user opt-in)
- `docker` (explicit opt-in, requires docker.sock mount)

### Docker support

**Decision: Support docker.sock mounting in v1 as opt-in capability.**

Rationale:

- Current `ssh_exec` users already run `docker ps`, `docker logs`, etc. Not supporting this is a regression.
- Docker-via-runner is actually _safer_ than Docker-via-SSH because:
  1. Runner can enforce read-only docker commands without giving full exec
  2. Every command is audited (you can't easily audit raw SSH)

Implementation:

- Runner Dockerfile: `docker.sock` mount is optional, controlled by user's `docker run` command
- Capability detection: runner reports `docker_available: true` in metadata on hello
- Server-side enforcement: if job uses docker command but runner lacks capability, reject early with clear error

### Read-only allowlist (initial)

For `exec.readonly`, allow commands that match a conservative allowlist:

- system read-only: `uname`, `uptime`, `date`, `whoami`, `id`, `df`, `du`, `free`, `ps`, `top -b -n 1`, `systemctl status`, `journalctl --no-pager --since ...`
- docker read-only (only if `docker` capability): `docker ps`, `docker logs --tail N`, `docker stats --no-stream`, `docker inspect`

Explicitly deny:

- any command containing obvious destructive verbs (best-effort): `rm`, `mkfs`, `dd`, `shutdown`, `reboot`, `useradd`, `chmod`, `chown`, `iptables`, `ufw`, etc.

Note: this is intentionally simple and imperfect; the product layer should eventually add explicit approval flows for "dangerous" classes rather than trying to parse shell perfectly.

---

## Observability & audit (must-have)

Write an audit row for every `runner_exec`, including:

- who: `owner_id`
- where: `runner_id` + runner metadata snapshot
- why: `worker_id` + `run_id` (already threaded through worker context)
- what: `command` (and later: policy stamp)
- result: `exit_code`, `duration_ms`, truncated stdout/stderr

This makes "what ran where, and why" answerable.

---

## Local dev impact

Once implemented, you can run a runner on your laptop and immediately regain:

- Tailscale reachability
- `~/.ssh/config` conveniences
- existing SSH keys/agent

…but the SaaS no longer needs direct access to those credentials.

This makes dev and prod behave more similarly.

---

## Migration plan (from today's `ssh_exec`)

1. Implement `runner_exec` and the runner daemon.
2. Update default worker tools to include `runner_exec`.
3. Keep `ssh_exec` temporarily as a fallback (dev only).
4. Remove:
   - hard-coded personal host allowlist in `ssh_exec`
   - `${HOME}/.ssh` mounts from production compose

End state: the SaaS does not assume ambient SSH identity.

---

## Decisions (resolved)

| Question              | Decision                            | Rationale                                                                                                                   |
| --------------------- | ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| **Naming**            | **Runner**                          | Avoids collision with existing OAuth connectors and agent domain model. Follows industry precedent (GitHub/GitLab runners). |
| **Artifact handling** | **Outputs only (v1)**, schema-ready | Keep scope tight. `artifacts` JSON field reserved in `runner_jobs` for future file upload support.                          |
| **Target resolution** | **Both ID and name**                | Name-based (`target="laptop"`) for ergonomics; ID-based (`runner:123`) for explicitness. Resolution is trivial.             |
| **Docker access**     | **Yes, opt-in capability**          | Parity with current `ssh_exec` usage. Actually safer via auditing. Requires explicit docker.sock mount.                     |
