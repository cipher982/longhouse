---
name: zerg-testing
description: Run, debug or stabilize Longhouse tests - backend, frontend, Rust engine, core E2E (Playwright), iOS dispatch lanes, simlab - and clean up after proof runs. Use when choosing which make target proves a change, attributing a test failure, fixing flaky E2E, or tearing down a QA run.
---

# Longhouse testing

Use Make targets rather than raw runners, and run the smallest test that proves
the invariant. `test-ci`, full E2E, visual suites and long Rust builds run in CI;
run them locally only to diagnose, during a CI outage or when David asks.
Cleanup is part of acceptance (see "Proof-run cleanup" below).

Backend tests live in `server/tests_lite/`: no shared conftest, one SQLite
database per test, HTTP overrides on `api_app` (not `app`), no environment or
settings read at import time. A bare `pytest tests_lite` must pass.

One heavy build at a time on this Mac (cargo, xcodebuild, Docker, release
validation): wrap it in `lockf /tmp/agents/longhouse-heavy-build.lock <command>`.
Each worktree builds its own cargo target; if cargo refuses a stale build
identity, run `python3 scripts/build/generate_build_identity.py`.

## Targets

| Command | Runs |
|---|---|
| `make test` | backend unit tests (`server/tests_lite`; `TEST=tests_lite/<file>.py` focuses) |
| `make test-frontend` | web unit tests plus type-check (`ARGS="src/path.test.ts"` focuses) |
| `make test-engine` | Rust engine (`TEST="mod::tests::name"` focuses) |
| `make test-runner` | runner |
| `make test-e2e-core` (alias `make test-e2e`) | launch-surface E2E plus accessibility, retries=0 |
| `make test-transcript-stability` | web and iOS frame-by-frame layout/scroll stability |
| `make validate` | every contract and drift check (pre-push gate) |
| `make test-ci` / `make test-full` | broad cutover runs; CI or diagnosis only |

Goals matching `ISOLATED_GOALS` (top of the `Makefile`: `test*`, `validate*`, `qa-*`,
`provider-*`, `ci-*` and others) never run on the host. They dispatch to a
disposable container, or to the wisp bench for native iOS and macOS. Run them on
their own, never in the same `make` invocation as a host goal like `dev` or `ship`.
Receipts and collected files land in `artifacts/test-isolation/<run-id>/`.

Heavy Docker-dispatched goals can go to the `ci` VM instead of the laptop lock:
`scripts/ops/remote.sh run make test` (`status` shows load; `--out PATH`,
`--env K=V`, `--timeout S`). Keep `release` and `validate-build-identity` on the
laptop and never run a provider CLI there.

## Named live checks against any commit: `make prove`

`make prove CELLS="claude/helm:tool pi_helm_steer_active" [SHA=<commit>]` runs
named real-provider cells against any commit (default `HEAD`, pushed or not) on
`cube-bench` (Linux, credential-free scripted model; never a provider CLI on
this Mac) and prints one `PASS|FAIL <cell> <seconds> <evidence path>` line per
cell, exit 1 on any failure. Use it for the check the pre-push gate does not run
(a real provider cell) and before waiting on a factory tick or the Terminal
Truth workflow.

- Cells: a Terminal Truth cell `provider/mode[:scenario]` (`claude/helm`,
  `codex/console:thinking`; scenarios `long_reply` (default), `two_message`,
  `thinking`, `tool`, `subagent`), or a factory producer assertion id
  (`pi_helm_steer_active`, `omp_helm_send_idle`). A producer runs its whole
  lifecycle once; all named cells of it share that run. Producers are
  allow-listed in `BENCH_PRODUCERS` (`scripts/ops/prove.py`) after a real run.
- The commit is fetched (or pushed from your checkout) into a bare repo on the
  bench; the engine and CLI come from `bin-cache` when the engine's inputs match
  an earlier build (`ls by-sha/<sha>` on the bench says which), else one build in
  a persistent worktree and cargo target. Providers are the CI-pinned versions
  (`scripts/ci/terminal-truth-providers.toml`).
- Teardown runs on success, failure, timeout and signal and the summary line says
  `cleanup clean` or `DIRTY` (counted as a failure). Evidence is copied to
  `/tmp/agents/prove/<run>/` and stays on the bench for 24 h.
- `TARGET=factory` and `TARGET=wisp` are not built and refuse by name.
  Measurements and open work: `control-plane/docs/specs/multi-agent-dev-loop-speed.md`.

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

## Playwright browsers

Longhouse keeps its own browser store (`~/Library/Caches/longhouse/playwright`,
shared by every worktree), not Playwright's global `ms-playwright` cache that other
repos on the machine also write to. `scripts/playwright-store.mjs` owns it: a ready
store is two `stat` calls, and a missing browser installs once under a heartbeat
lock with a progress watchdog and a private temp dir. Never run `bunx playwright
install` here: it takes the shared global lock, installs under whatever Node is
first on PATH, and has hung on this Mac for days. Playwright 1.57 never finishes
extracting under Node 26 (the download hits 100%, then nothing), so the store runs
installs under Bun.

- Any script or config that imports Playwright starts with
  `import "<rel>/scripts/playwright-store-auto.mjs"` (`-webkit` for WebKit). It
  pins the store path before `HOME` can be scratched and self-heals a missing
  browser. `scripts/tests/playwright-store.test.mjs` fails the build if one is missing.
- `make ensure-playwright-browser` is the manual form; `node scripts/playwright-store.mjs
  status` reports without installing. A stalled install is killed after 180s without
  progress and says why. Inside the test container the image's `/opt/playwright`
  wins and nothing is downloaded.

## iOS lanes

Agents own iOS build, test and evaluation; Xcode.app is never in the loop. Local
lanes run on the bench (`scripts/ops/bench.sh run <cmd>`); `make ios-unit` is the
local unit loop. Use the simulator first and a real device only for device-only
behavior. iOS CI does not gate deploys, so check the iOS job yourself when you
touch `ios/`. David's iPhone is not a test device; install there last
(`make phone-deploy`).

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

## Waiting on GitHub

Never run `gh run watch` or a `gh run view`/`gh run list` loop faster than every
45 s: every personal token of the cipher982 account shares one 5000/h budget, and
agent watchers exhausted it on 2026-11-15. Wait with `make ship-watch SHA=<sha>`
or for the Sauron ship watcher's message. `gh api rate_limit` is free.

## Proof-run cleanup

Whoever starts a server, engine, provider process, poll loop, browser tab,
simulator, scratch `HOME`, hosted test tenant or proof session owns it through
teardown on success, failure, timeout and cancellation, including what a
subagent or child agent started. Preserve the maintainer's own simulators and services.
The specifics:

- Give every background loop a completion condition and a cap. Never `curl -L`
  a `longhouse.ai` URL in a loop (redirects make it spin).
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
  (`managed-provider-cli`, which says why), with `--no-session` or a disposable `--session-dir`.
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
  inventory, the hosted session inventory and `xcrun simctl list devices`. A
  green test or an exited wrapper is not proof. Report what you kept on purpose
  and who owns it.
- Hosted test tenants: deprovision and erase them, and undo any allowlist or
  credential change (`control-plane/docs/runbooks/hosted-operations.md`).
- Test sessions declare a hidden `launch_surface` (`test`, `e2e`, `product-e2e`,
  `qa`, `ci`, `canary`, `factory_assurance`) on `POST /api/agents/sessions`; read
  them back with `include_test=true`. Hidden is not cleanup: retire the session
  (`ended_at`, no owned runs, absent from inventory) and prove provider and
  Machine Agent processes are dead.
