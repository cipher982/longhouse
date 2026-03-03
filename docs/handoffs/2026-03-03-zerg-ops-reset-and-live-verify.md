# Handoff: zerg-ops Reset + Live Verify (2026-03-03)

## Situation
User called out backup/offsite work as over-complex and unacceptable (too many flags/env vars, and personal infrastructure values in source). Required direction: simplify aggressively, code-configure policy, remove personal endpoint details from repo, and keep strict end-to-end backup/verify guarantees.

## Current State

| Area | State |
|---|---|
| Repo branch | `main` |
| Backup simplification commit | `f14ddf45` pushed to `origin/main` |
| Local backup contract test | Pass (`make test-zerg-ops-backup`) |
| Engine roundtrip guard (Claude) | Pass (`cargo test ... test_ship_unship_roundtrip_claude_fixture`) |
| Engine roundtrip guard (Codex) | Pass (`cargo test ... test_ship_unship_roundtrip_codex_fixture`) |
| zerg host script | `/usr/local/bin/zerg-ops` updated from repo script |
| zerg timers | `zerg-ops.timer` active, `zerg-ops-monitor.timer` active |
| zerg monitor service run | Manual run passed (`checked_instances=2`) |
| Offsite target in repo | Removed personal host/IP/path from source |
| Offsite runtime mapping | Host SSH alias `longhouse-offsite` in `/root/.ssh/config` |
| Offsite parity check | `david010` local and remote latest archive sizes equal (`2218836158`) |

## Key Discoveries
1. `scripts/zerg-ops.sh` had grown into a config framework with broad flag surface and still included personal remote target (`drose@100.98.103.56`) in source. This was the core failure mode.
2. The minimal acceptable contract is:
   - full SQLite snapshot via backup API
   - mandatory restore verification (hash + integrity + counts)
   - retention pruning
   - monitor freshness + offsite parity
3. Offsite endpoint details should not live in git. Neutral alias in code + host-only SSH config cleanly separates product code from personal infrastructure details.
4. Systemd units were still loading `/etc/zerg-ops.env`; this had to be removed to match the new code-configured contract.
5. Local test originally expected 2 snapshots, but new policy is fixed `KEEP_SNAPSHOTS=14`; test updated to run enough rounds (16) and assert 14 retained.

## Decisions Made & Why
1. **Removed env-file contract** (`/etc/zerg-ops.env`) from repo design.
   - Why: user explicitly rejected env-var sprawl and wants operations behavior encoded in code.
2. **Kept a tiny CLI surface only** (`--instance`, `--no-offsite`, test path overrides).
   - Why: operationally necessary for scoped runs/tests, without reopening general config complexity.
3. **Offsite in code uses neutral alias** `longhouse-offsite`.
   - Why: no personal host/IP in source, but still deterministic operational behavior.
4. **Removed `scripts/zerg-ops.env.example`**.
   - Why: it no longer reflects supported configuration.
5. **Updated AGENTS/spec/TODO** to reflect code-configured backup model.
   - Why: prevent future re-introduction of env-driven complexity.

## What Changed (Repo)
- Updated: `scripts/zerg-ops.sh`
- Updated: `scripts/test-zerg-ops.sh`
- Updated: `docs/specs/zerg-ops-backup.md`
- Updated: `AGENTS.md`
- Updated: `TODO.md`
- Deleted: `scripts/zerg-ops.env.example`

Commit pushed:
- `f14ddf45 infra: simplify zerg-ops and remove env-driven backup config`

## What Changed (Live on zerg)
1. Deployed script:
   - `scp scripts/zerg-ops.sh zerg:/tmp/zerg-ops.sh`
   - `sudo install -m 0755 /tmp/zerg-ops.sh /usr/local/bin/zerg-ops`
2. Added host-only offsite alias in `/root/.ssh/config`:
   - `Host longhouse-offsite`
   - `HostName 100.98.103.56`
   - `HostKeyAlias 100.98.103.56`
   - `User drose`
   - `IdentityFile /root/.ssh/longhouse_bremen_backup`
3. Updated systemd services to remove env-file dependency:
   - `/etc/systemd/system/zerg-ops.service` now `ExecStart=/usr/local/bin/zerg-ops run`
   - `/etc/systemd/system/zerg-ops-monitor.service` now `ExecStart=/usr/local/bin/zerg-ops monitor`
4. Reloaded/restarted timers:
   - `sudo systemctl daemon-reload`
   - `sudo systemctl restart zerg-ops.timer zerg-ops-monitor.timer`
5. Removed obsolete config file:
   - `sudo rm -f /etc/zerg-ops.env`

## Verification Evidence
1. `make test-zerg-ops-backup` => `PASS: zerg-ops backup/restore retention contract`
2. `cargo test -p longhouse-engine test_ship_unship_roundtrip_claude_fixture` => 1 passed
3. `cargo test -p longhouse-engine test_ship_unship_roundtrip_codex_fixture` => 1 passed
4. Live backup run:
   - `sudo /usr/local/bin/zerg-ops backup --instance david010 --instance david-stripetest`
   - produced verified snapshots and offsite sync complete for both instances
5. Live monitor run:
   - `sudo /usr/local/bin/zerg-ops monitor --instance david010 --instance david-stripetest`
   - `monitor success checked_instances=2`
6. Offsite size parity check (`david010` latest archive):
   - local: `2218836158`
   - remote: `2218836158`

## How to Work on This Next
1. Treat `scripts/zerg-ops.sh` as an opinionated policy file, not a generic backup framework.
2. If offsite endpoint changes, update host SSH alias `longhouse-offsite` on zerg; do not commit host/IP/path values to repo.
3. Keep contract tests strict around byte/hash restore verification and retention behavior.
4. If adding options, force justification against complexity creep; default answer should be "no" unless operationally mandatory.

## Next Steps
1. Optionally add one targeted unit/smoke test asserting no env-file loading behavior exists in `zerg-ops.sh` (guard against regressions back to env-surface).
2. If required by user, run full `make test` and `make test-e2e` (not run in this session).
3. Keep monitoring `zerg-ops-monitor.timer` journals over next 24h for first post-reset cycle:
   - `journalctl -u zerg-ops-monitor.service -n 200 --no-pager`

## Reference
- Repo script: `scripts/zerg-ops.sh`
- Repo contract test: `scripts/test-zerg-ops.sh`
- Spec: `docs/specs/zerg-ops-backup.md`
- Commit: `f14ddf45`
- Live script path: `/usr/local/bin/zerg-ops`
- Live timers:
  - `zerg-ops.timer`
  - `zerg-ops-monitor.timer`
- Live services:
  - `/etc/systemd/system/zerg-ops.service`
  - `/etc/systemd/system/zerg-ops-monitor.service`
- Host SSH alias config:
  - `/root/.ssh/config` (`Host longhouse-offsite`)

## Known Unrelated Local State
Untracked files present locally and intentionally untouched:
- `apps/zerg/backend/tests/__init__.py`
- `docs/handoffs/2026-03-02-parser-fidelity.md`
