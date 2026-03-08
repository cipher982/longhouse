# Runner Doctor v1

## Goal

Make runner failures obvious and fixable without teaching users `launchd`, `systemd --user`, `systemctl`, or `loginctl`.

This is a **diagnose-first** feature, not a magic self-healing framework.

## Research Takeaways

The good "doctor" tools all share the same shape:
- Run a small, named set of checks.
- Mark each check clearly as pass / warn / fail.
- Explain the problem in plain language.
- Give one concrete next step.
- Keep mutation separate from diagnosis.

For Longhouse, that means:
- `doctor` should explain what Longhouse sees and what the machine sees.
- `repair` in v1 should mean **generate the correct re-enroll / reinstall command**, not hidden remote mutations.
- Avoid multiple fallback paths. There should be one obvious repair path per runner.

## First Principles

### What users actually need

When a runner breaks, users want answers to four questions:
1. Is this runner online right now?
2. If not, why not?
3. Is the problem on the machine or in Longhouse config?
4. What exact command should I run to fix it?

### What Longhouse can know remotely

From the control plane, we can reliably know:
- current runner status (`online`, `offline`, `revoked`)
- last seen time
- configured capabilities
- last reported runner metadata

That is enough for a **server-side doctor** with reason codes.

### What only the machine can know

From the machine itself, we can reliably know:
- whether local config/env exists
- which install mode is expected (`desktop` or `server`)
- whether the service definition exists
- whether the service is active
- whether the instance health endpoint is reachable

That is enough for a **local CLI doctor**.

## Product Shape

### 1. Server-side doctor

Add `GET /api/runners/{id}/doctor`.

It returns:
- overall severity: `healthy | warning | error`
- summary sentence
- stable reason code
- install mode (or `unknown`)
- recommended repair mode (`desktop | server`)
- a small list of named checks

Examples of v1 reason codes:
- `healthy`
- `runner_revoked`
- `runner_never_connected`
- `runner_offline_recently_seen`
- `runner_capabilities_mismatch`
- `runner_needs_reenroll`
- `runner_metadata_incomplete`

### 2. Repair command generation

Do **not** add a bespoke repair endpoint in v1.

Instead:
- reuse `POST /api/runners/enroll-token`
- generate a native install command for the existing runner name
- pass the detected install mode (`desktop` or `server`)

That keeps the repair path identical to the supported installer path.

### 3. UI doctor

Add a `Run Doctor` button on the runner detail page.

The UX should show:
- summary
- named checks
- recommended action
- `Generate Repair Command`
- copyable command block

The repair button should only appear when the doctor says action is needed.

### 4. Local CLI doctor

Add:
- `longhouse-runner doctor`
- `longhouse-runner doctor --json`

Checks in v1:
- config found
- config valid
- install mode detected
- expected service definition present
- service active
- Longhouse `/api/health` reachable

The CLI should not mutate anything in v1.

## Explicit Non-Goals

Not in v1:
- automatic service restarts from the web UI
- hidden self-healing logic
- remote `sudo` escalation
- reboot orchestration
- firewall/network repair
- SSH bridge integration
- version drift auto-updates

## Minimal Data Additions

Runner metadata should include:
- `install_mode`

Installer env files should persist:
- `RUNNER_INSTALL_MODE`

That gives the server enough signal to recommend the right repair path.

## Rollout Order

### Slice A
- add `RUNNER_INSTALL_MODE` persistence and metadata
- add backend doctor endpoint + tests

### Slice B
- add `Run Doctor` UI + repair command generation

### Slice C
- add local `longhouse-runner doctor` + Bun tests

## Progress Log

- 2026-03-08: Initial spec written from first principles. Keep v1 diagnose-first and use reinstall / re-enroll command generation as the single repair path.
