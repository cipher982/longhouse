---
name: zerg-testing
description: Run, debug or stabilize Longhouse tests - backend, frontend, Rust engine, core E2E (Playwright), iOS dispatch lanes, simlab - and clean up after proof runs. Use when choosing which make target proves a change, attributing a test failure, fixing flaky E2E, or tearing down a QA run.
---

# Longhouse testing

`AGENTS.md` ("Build and test", "QA ownership and cleanup") owns the rules: Make
targets only, smallest test that proves the invariant, one heavy build at a time,
cleanup is part of acceptance. This skill adds the mechanics.

## Targets

| Command | Runs |
|---|---|
| `make test` | backend unit tests (`server/tests_lite`) |
| `make test-backend-single TEST=tests_lite/<file>.py` | one backend file or node |
| `make test-frontend` | web unit tests plus type-check (`ARGS="src/path.test.ts"` focuses) |
| `make test-engine` / `make test-engine-single TEST="mod::tests::name"` | Rust engine |
| `make test-runner` | runner |
| `make test-e2e-core` (alias `make test-e2e`) | launch-surface E2E plus accessibility, retries=0 |
| `make test-e2e-single TEST=tests/<spec>.ts` | one E2E spec (`VERBOSE=1` adds list, HTML and JUnit reporters) |
| `make validate` | every contract and drift check (pre-push gate) |
| `make test-ci` / `make test-full` | broad cutover runs; CI or diagnosis only |

Goals matching `ISOLATED_GOALS` (top of the `Makefile`: `test*`, `validate*`, `qa-*`,
`provider-*`, `ci-*` and others) never run on the host. They dispatch to a
disposable container, or to the wisp bench for native iOS and macOS. Run them on
their own, never in the same `make` invocation as a host goal like `dev` or `ship`.
Receipts and collected files land in `artifacts/test-isolation/<run-id>/`.

Heavy Docker-dispatched goals can go to the crunch VM instead of the laptop lock:
`scripts/ops/crunch.sh run make test` (`status` shows load; `--out PATH`,
`--env K=V`, `--timeout S`). Keep `release` and `validate-build-identity` on the
laptop and never run a provider CLI on crunch.

## Attributing a failure first

- **Running tests from inside a managed session gives false failures.**
  `LONGHOUSE_MANAGED_SESSION_ID`, `LONGHOUSE_COORDINATION_TOKEN` and
  `LONGHOUSE_RUN_ID` in the shell make MCP and coordination clients attach extra
  headers, so tests asserting exact request arguments fail. Re-run with them unset.
- `test_raw_object_workers.py::test_broken_pool_cleanup_terminates_surviving_owned_child`
  is load-sensitive: passes alone, can fail in the full suite.
- A focused selection is not a gate for edits to shared budgets, response
  constructors or a module `__all__` (an edit there once replaced a neighbouring
  line instead of adding one). Run the full `tests_lite`, core E2E and
  `make validate-sdk`.
- New routes or response fields fail `validate-sdk` until `make generate-sdk`;
  regenerate in the same change.

## E2E

- Read failures first: `artifacts/test-isolation/<run-id>/files/` holds `errors.txt`,
  `summary.json` and `test-results/`. Then `make test-e2e-single`, then `VERBOSE=1`.
- Fix by cause: selector drift gets a `data-testid` in React; readiness waits on a
  network response, not optimistic UI; shared state is isolated per test and
  worker; helpers fail loudly; wait on conditions, never sleeps.
- Core E2E stays at retries=0. A CI failure that passes on rerun with no code diff
  is quarantined or moved out of the blocking lane the same day, with a tracking
  issue. Core E2E does not gate a runtime deploy, but a red run still owes that
  decision.

## iOS lanes

`make test-ios`, `ios-previews`, `ios-ui-shot`, `simlab-run` and `menubar-harness`
dispatch to the wisp runner (one job at a time under the bench lock, scratch
`HOME`). They need an authenticated `gh`, ssh to `wisp`, and a clean, pushed
revision; a dirty worktree is refused and there is no local fallback.

- Run them from the laptop, never through `bench.sh` (it holds the lock the job
  waits for). The bench is for lanes that build and boot locally: `sim.sh`,
  `simlab.py up/run`, `phone.sh`.
- The run's `head_sha` is `main`; wisp checks out your `source_sha`. Read the
  `source=<sha>` line the dispatcher prints.
- CI splits the merge gate over two lanes with `IOS_TEST_SCHEMES` and
  `IOS_TEST_FILTER` (`scripts/ci/run_ios_tests.sh`). `make test-ios-helper` fails
  when the lanes stop covering the gate. A filtered `make test-ios` says
  `PARTIAL run`; only the unfiltered run is the gate.
- `make ios-unit` runs hermetic unit tests on a local simulator. It is an
  iteration loop, not the gate.

## Recovery and terminal fidelity

- Transcript delivery, stale client content, reopen, network recovery:
  `make simlab-run` (focus with
  `SCENARIOS="interrupted-client-recovery client-network-recovery"`). Details,
  isolation and limits: [zerg-ui simlab](../zerg-ui/SKILL.md#autonomous-recovery-dogfood-simlab).
  Server counts or an early render do not prove the client got the final reply.
- Real-provider terminal fidelity: `make test-console-served-state-e2e` with
  `make test-terminal-fidelity-web` and `make test-terminal-fidelity-ios`
  ([maintainer testing runbook](../../../docs/runbooks/maintainer-testing.md)). Real execution,
  rendered pixels and connection recovery are separate proof obligations.

## Proof-run cleanup

AGENTS.md sets the ownership rule. The specifics:

- Create every test or QA session with an explicit hidden `launch_surface`; never
  rely on a human-looking title. Launch-hidden is not timeline-hidden.
- A QA cleanup receipt passes only after each run-owned Runtime Host session is
  hidden on the timeline, archived, and absent from the served agent inventory
  (`include_test=true` still shows a merely launch-hidden one). Record
  `canary_session_hidden: true`.
- Allocate unique `mktemp -d` roots, record every path and PID/PGID you own, and
  install an `EXIT/INT/TERM` trap. No fixed `/tmp` names.
- Reparented descendants (provider, bridge, shipper, catalog daemon, server with
  parent PID 1) are still yours. Stop them through the ownership record and
  re-read the inventory after the controller exits.
- Provider probes run on the bench or the factory, never on this Mac
  (`managed-provider-cli`), with `--no-session` or a disposable `--session-dir`.
  If a protocol ignores that, record the native session path and remove it in
  `finally`.
- Cleanup never depends on a downstream archive, backup or ingest worker.
  Delete a failed proof's evidence only after required source retention is
  verified. If retention fails, keep the isolation, mark cleanup failed, and
  report owner and removal condition.
- Keep only durable fixtures and the required receipt under ignored `artifacts/`;
  report anything retained with its owner and cleanup command. A screenshot does
  not replace closing the window, tab or simulator it came from.
- Before declaring done, re-read `git status --short`, the owned process
  inventory, the hosted session inventory and `xcrun simctl list devices`.
