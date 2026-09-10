---
name: zerg-testing
description: Zerg testing workflow (unit + E2E). Use when running or debugging tests.
---

# Zerg Testing

## Rules
- Always use Make targets. Never run pytest/bun/playwright directly.


## Cleanup contract
- A proof run is incomplete while its provider process, Longhouse session,
  simulator/browser tab, scratch `HOME`, relay, or dev service is alive.
- Create every test/QA session with an explicit hidden `launch_surface`; never
  use a human-looking project/title and rely on naming to hide it.
- Provider probes must use `--no-session` or an explicitly disposable
  `--session-dir`; never let a verification command write to David's default
  provider archive. If a protocol ignores that flag, record the exact native
  session path and remove it in the same `finally` block.
- In `finally`/cleanup, stop owned processes and services, close tabs/simulators,
  remove scratch homes and generated fixtures, then re-read the session/process
  inventory. Do not leave a verification transcript in David's user timeline.
- Keep only durable source fixtures and the receipt required by the proof. Report
  any intentionally retained artifact, owner, and cleanup command.
## Core Commands
```bash
make test                # unit tests
make test-e2e-core       # core E2E (must pass 100%)
make test-e2e            # full E2E (retries ok)
make test-full           # unit + full E2E + visual checks
make test-e2e-single TEST=tests/<spec>.ts
make test-e2e-errors     # show last E2E errors
make test-e2e-verbose    # full output for debugging
```

## Real-client recovery, without a phone

For transcript delivery, stale client content, app reopen, or network recovery,
start with `make simlab-run`; focused recovery uses
`SCENARIOS="interrupted-client-recovery client-network-recovery"`.
`make test-ios-helper` covers harness verdict/cleanup boundaries and the real
TCP relay. Read the [zerg-ui simlab workflow](../zerg-ui/SKILL.md#autonomous-recovery-dogfood-simlab)
for isolation, screenshots, retained evidence, and limits. Server counts or
an early render are not sufficient proof that the client received the final reply.

For real-provider terminal fidelity, pair `make test-console-served-state-e2e`
with `make test-terminal-fidelity-web` and `make test-terminal-fidelity-ios`.
The root [CONTRIBUTING.md Tests section](../../../CONTRIBUTING.md#tests) documents
the case manifest, explicit target/authentication, screenshots and source
immutability. These complement simlab; real execution, rendered pixels and
connection recovery are separate proof obligations.

## Debugging Flow
1) `make test-e2e-errors`
2) `make test-e2e-single TEST=tests/<spec>.ts`
3) `make test-e2e-verbose`

## Flake Policy
- Keep core E2E at retries=0. If a CI failure passes on rerun with no code
  diff, quarantine or move that test out of the blocking lane the same day and
  leave a tracking issue; do not normalize red-but-ignored CI.
