---
name: zerg-testing
description: Zerg testing workflow (unit + E2E). Use when running or debugging tests.
---

# Zerg Testing

## Rules
- Always use Make targets. Never run pytest/bun/playwright directly.
- `test*`, `validate*`, `qa-*`, `provider-*`, `ci-*` and the other goals in
  `ISOLATED_GOALS` (top of the Makefile) never run on the host: they dispatch to
  a disposable Docker container, or for native iOS/macOS targets to a hosted macOS
  VM (below). Receipts and collected files land under
  `artifacts/test-isolation/<run-id>/`. Run them on their own, never in the same
  `make` invocation as a host goal such as `dev` or `ship`.
- One heavy build at a time on this Mac (cargo, Docker, xcodebuild, release
  validation): wrap it as `lockf -k /tmp/agents/longhouse-heavy-build.lock <cmd>`
  (`release.sh` takes the same lock for its validation only, so never wrap
  `make release`). Docker-dispatched goals can instead go to the crunch VM,
  `scripts/ops/crunch.sh run make test` (`status` shows load; `--out PATH`,
  `--env K=V`, `--timeout S`; the workspace AGENTS.md has the rules). Keep goals
  that stamp git identity (`release`, `validate-build-identity`) on the laptop,
  and never run a provider CLI on crunch.
- A proof run is incomplete while its provider process, Longhouse session,
  simulator/browser tab, scratch `HOME`, relay, or dev service is alive.
- Create every test/QA session with an explicit hidden `launch_surface`; never
  use a human-looking project/title and rely on naming to hide it. Hidden launch
  provenance is not the same as user timeline hiding.
- Before a QA cleanup receipt can pass, retire every exact Runtime Host session
  owned by the run: set timeline visibility to hidden, archive it, and verify
  the session ID is absent from the served agent inventory. Record
  `canary_session_hidden: true` in the receipt. `include_test=true` diagnostic
  surfaces can still expose a merely launch-hidden session.
- Provider probes (running a provider CLI to see what it does) run on the bench
  or the provider factory, never on the maintainer's Mac (`managed-provider-cli`).
  Wherever they run they use `--no-session` or an explicitly disposable
  `--session-dir`; never let a verification command write to a default provider
  archive. If a protocol ignores that flag, record the exact native
  session path and remove it in the same `finally` block.
- Every disposable proof run must allocate unique `mktemp -d` roots, record every path/PID it owns, and install an `EXIT/INT/TERM` trap; fixed `/tmp` names and untracked scratch files are prohibited.
- Treat reparented descendants as still run-owned: a provider, bridge, shipper,
  catalog daemon, or verification server whose parent is PID 1 is not cleaned
  up. Record exact PID/PGID at launch, stop through that ownership record, and
  re-read the exact inventory after the controller exits; never infer teardown
  from a downstream worker or archive receipt.
- Before handoff, delete every exact run-owned path and verify no run-owned process, session, simulator, tab, or service remains; retain only the requested receipt under ignored `artifacts/`.
- In `finally`/cleanup, stop owned processes and services, retire the hosted
  session, close tabs/simulators, remove scratch homes and generated fixtures,
  then re-read the session/process inventory. Do not leave a verification
  transcript in David's user timeline.
- Cleanup must not depend on a downstream archive, backup, or ingest worker.
  Those are evidence-delivery paths, not ownership or teardown authorities.
- Keep only durable source fixtures and the receipt required by the proof.
  Report any intentionally retained artifact, owner, and cleanup command.
- A failed or superseded proof may be deleted only after its required authoritative source retention is verified; then delete its exact evidence root, request file, scratch `HOME`, native session/archive, and owned processes. Retain only the required receipt under ignored `artifacts/`, with its owner and cleanup command.
- If authoritative retention fails, preserve the disposable isolation as evidence, mark cleanup failed, and report its exact owner and removal condition; never delete the only source and call cleanup complete.
- Rendered frames and verification transcripts are evidence, not durable user-facing artifacts: keep only the required receipt under ignored artifacts and remove named QA windows/sessions before handoff. A screenshot is never a cleanup substitute; any visible simulator, provider TUI, browser tab, or verification conversation opened by the run must be closed in that run's `finally` path, or the run remains incomplete.
- Cleanup is a release gate, not a follow-up: re-read `git status --short`, the exact owned PID/process-group inventory, hosted session inventory, and `xcrun simctl list devices` before declaring the run complete.
## Attributing a failure before you chase it

- **Running the suite from inside a managed session produces false failures.**
  `LONGHOUSE_MANAGED_SESSION_ID` / `LONGHOUSE_COORDINATION_TOKEN` /
  `LONGHOUSE_RUN_ID` in the ambient shell make the MCP and coordination clients
  attach `X-Agents-Token` and `X-Longhouse-Session-Id`, so tests asserting exact
  request arguments fail on the extra headers. Re-run with those unset before
  concluding anything.
- `test_raw_object_workers.py::test_broken_pool_cleanup_terminates_surviving_owned_child`
  is load-sensitive: it passes in isolation and can fail under the full suite.
- **A focused selection is not a gate.** Regressions have shipped in changes
  whose own tests passed, because an edit replaced a neighbouring line instead of
  adding a sibling: a shared budget dict lost an entry, a response constructor
  lost two keyword arguments, and a module `__all__` lost a name. Anything
  touching those three shapes needs the full `tests_lite`, the full core E2E,
  and `make validate-sdk`.
- New routes or response fields fail `validate-sdk` until `make generate-sdk`
  runs; regenerate in the same change.

## Core Commands
```bash
make test                # backend unit tests (server/tests_lite)
make test-backend-single TEST=tests_lite/<file>.py
make test-frontend       # web unit tests + type-check (ARGS="src/path.test.ts" focuses)
make test-engine         # Rust engine (TEST="mod::tests::name" via test-engine-single)
make test-runner         # runner
make test-e2e-core       # launch-surface E2E plus accessibility, retries=0 (must pass 100%)
make test-e2e            # same lane (core + a11y, one backend boot)
make test-e2e-single TEST=tests/<spec>.ts
make validate            # every contract/drift check (the pre-push gate)
make test-ci             # broad cutover: validate, import-smoke, test, frontend, runner, engine, wheel, shipper E2E
make test-full           # test, frontend, runner, engine, shipper E2E, E2E
```

## The iOS lane is a dispatch, not a local run

`make test-ios`, `make ios-previews`, `make ios-ui-shot`, `make simlab-run` and
`make menubar-harness` submit the work to a fresh GitHub-hosted macOS VM. They need an **authenticated `gh` on the machine
that runs them** (repo visibility, pushed-SHA proof, submission, run
reconciliation) and a **clean, pushed revision**; an uncommitted worktree is
refused by design, and there is no local native fallback for fixtures.

- Run dispatched targets from the laptop, which holds the `gh` auth, or from the
  bench once its credential file is provisioned: `bench.sh` loads
  `~/.config/longhouse/bench.env` on that host when it exists (a GitHub
  credential; rotate by rewriting the file). The
  bench is otherwise for the lanes that build and boot locally: `sim.sh`,
  `simlab.py up/run`, `phone.sh`.
- A dispatched run's own `head_sha` is `main` while the VM checks out your
  `source_sha`. Read the `source=<sha>` line the dispatcher prints, not the run's
  SHA, when asking what was tested.
- CI splits the merge gate over two class-split lanes with `IOS_TEST_SCHEMES` /
  `IOS_TEST_FILTER` (entries `Scheme:only:Id` / `Scheme:skip:Id`; see
  `scripts/ci/run_ios_tests.sh`): the unit scheme plus the small UI classes, and
  `SessionChatUITests` alone. Each lane repeats the test plan's own skips, and
  `scripts/tests/ios-test-lanes.test.py` (run by `make test-ios-helper`) fails when
  the lanes stop covering the merge gate. `make test-ios IOS_TEST_FILTER=...`
  runs part of it and says so (`PARTIAL run`); only the unfiltered run is the gate.
- Hermetic unit tests do not need the VM: `make ios-unit` runs them on a local
  simulator in ~35s of tests (~80s cold). It is a host development goal beside
  `sim-deploy` and `phone-deploy`, and an iteration loop, not the gate -- the
  dispatched lane above owns the merge and also runs the smoke scheme and the
  fixtures that need the disposable VM.

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
1) Read the failed run's collected evidence under `artifacts/test-isolation/<run-id>/files/`
   (`errors.txt` and `summary.json` from the minimal reporter, plus `test-results/`)
2) `make test-e2e-single TEST=tests/<spec>.ts`
3) Add `VERBOSE=1` for the list, HTML and JUnit reporters

## Flake Policy
- Keep core E2E at retries=0. If a CI failure passes on rerun with no code
  diff, quarantine or move that test out of the blocking lane the same day and
  leave a tracking issue; do not normalize red-but-ignored CI. Core E2E no longer
  gates a *runtime deploy* (a deploy waits on backend, engine, frontend+runner and provider-contract tests,
  and the deploy gate warns about E2E instead), but a red E2E is still a red CI run
  and still owes that same-day decision.
