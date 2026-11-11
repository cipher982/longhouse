# Maintainer testing and proofs

Detail behind the test tiers in [`CONTRIBUTING.md`](../../CONTRIBUTING.md#tests):
how test isolation works, how native and live proofs are dispatched, and what
evidence they leave. Most contributors never need this page.

## Test isolation

Ordinary `test-*`, `validate-*`, `qa-*` and onboarding-funnel targets run in a
disposable container. Local runs need Docker. The supervisor copies the current
source into a container with no host mounts, ambient credentials or external
network. Dependency preparation has network access and receives only package
manifests. Browser binaries come from the official Playwright image pinned to
the version in `bun.lock`.

- Receipts and evidence go under `artifacts/test-isolation/<run-id>/`.
- Each run has its own container and private home. The container deadline
  survives a killed supervisor, and the next run removes expired resources owned
  by that checkout without touching active runs or other checkouts.
- Never pass provider credentials to fixture targets.
- Push CI builds the manifest-keyed dependency image once on a self-hosted
  runner and caches it. The image is also published to GHCR by manifest tag for
  lanes that pull it. Pull credentials stay in the supervisor's temporary Docker
  config. A missing or mismatched requested digest fails instead of falling back
  to a cold build.
- CI uses no GitHub Actions artifacts or caches. A fact one run records for a
  later gate (QA verdict, engine-compat result, image publication) is a CI
  receipt in the artifact object store (`scripts/ops/ci_receipts.py`). Evidence
  about a failed run stays on the runner that produced it.
  `make validate-actions-storage` fails on new `upload-artifact`,
  `download-artifact`, `actions/cache` or `type=gha`; the one exception is in
  `scripts/ci/actions-storage.allow`.

`make affected-check BASE=<base-sha>` lists changed files, CI lanes and local
commands from `.github/path-filters.yml`; add `RUN=1` to run supported targets.
It includes staged, unstaged and untracked files, so it can be broader than
CI's committed diff. An unmatched path needs explicit review and is not a green
test. Full `make test-ci` is a deliberate broad check, not the default pre-push step.

For a pushed maintainer SHA, `make ship-watch SHA=<full-sha> ARGS=--json` waits
for the exact workflow and deployment disposition and returns workflow URLs.
`no_runtime_change` means no hosted mutation occurred, not that another SHA was promoted.

## Native lanes (iOS, macOS, installer, WebKit)

Native iOS, macOS packaging, installer and WebKit onboarding fixtures run only
for an exact pushed SHA on a native runner: the `wisp` bench Mac, a self-hosted
Actions runner. A GitHub-hosted macOS VM is still accepted if the repository
goes public. Credentialed macOS jobs (signing, notarization, TestFlight) run
there too. TestFlight signs with Apple's cloud-managed certificate, so no
signing identity lives on wisp.

- CI calls `python3 scripts/qa/native-test-bootstrap.py --target TARGET` with
  explicit `NATIVE_OPTIONS_JSON`. It takes the bench lock (one job at a time),
  creates a disposable home, deletes the simulators it created, and records
  `$RUNNER_TEMP/native-isolation-evidence/`. There is no local native fallback:
  the bootstrap refuses any other self-hosted Mac.
- Dispatched runs leave evidence in `/tmp/longhouse-native-evidence/<request id>/`
  on wisp, and the dispatcher copies it back over ssh. Read it and its receipt
  when a native fixture fails.
- Dispatching from a workstation (`make test-ios`, `make ios-previews`,
  `make ios-ui-shot`, `make simlab-run`, `make menubar-harness`) needs an
  authenticated `gh` on that host and ssh to `wisp`. The dispatcher proves the
  revision is pushed, submits the workflow, reconciles its run and copies the
  evidence back. Run these from the machine that holds the credential, never
  through `bench.sh`, which holds the lock the dispatched job waits on.
- The workflow definition comes from `main` while the VM checks out your
  `source_sha`, so the run's own `head_sha` is `main`. Read the `source=<sha>`
  line the dispatcher prints.
- CI runs the iOS merge gate as two lanes split by test class: the unit scheme
  plus small UI classes, and `SessionChatUITests` alone (`IOS_TEST_SCHEMES` and
  `IOS_TEST_FILTER`, entries `Scheme:only:Id` or `Scheme:skip:Id`).
  `scripts/tests/ios-test-lanes.test.py` fails when the lanes stop covering the
  Makefile's merge-gate schemes. `make test-ios IOS_TEST_FILTER=...` runs part
  of the gate and says so.
- `make ios-unit` runs the hermetic unit target on a local simulator. It is a
  development loop, not the gate; the dispatched lane also runs the smoke
  scheme and the fixtures that need the disposable VM.
- iOS image-send tests assert the MIME type inside the attachment part's header
  block, since a match anywhere in the body misses malformed parts. Console
  send and reconciliation UI tests assert rendered row counts and statuses, then
  attach a frame with `make ios-ui-shot TEST=<Suite>/<test>`.

## Session status and motion

The web session and the local replay studio share the Ledger renderer. Start
`make live-status-lab`, then `make capture-ledger-journeys` to exercise the
production session route against isolated HTTP/SSE fixtures. It checks activity
expiry, a silent open connection, recovery, host and transcript uncertainty,
approval, draft retention, completion, narrow layouts and reduced motion without
issuing provider commands.

For recorded transcript content, run `make capture-live-status SESSION=<id>`,
then pass `CAPTURE=<private.json>` to the journey target or to
`make capture-live-status-frames SURFACE=ledger`. Captures contain private
session content: keep frames, videos and manifests local and untracked. Look at
the frames as well as the assertions, then stop the lab process you started.
For native status layouts, use `make ios-previews` and `make ios-ui-shot`.

## Native pipeline recovery

`make simlab-run` dispatches a clean pushed-SHA native job off the laptop.

```bash
make simlab-run SCENARIOS="interrupted-client-recovery client-network-recovery"
make test-ios-helper
```

A verdict needs the final synthetic source reply in the server projection and a
matching client-render acknowledgement; server-only progress cannot pass.
Screenshots, logs, projections, timings and verdicts are retained in the native
CI artifact and downloaded under the run's `artifacts/test-isolation/`
directory. Read the screenshots as well as the verdict. The loopback relay
models connection loss, not cellular hardware, and app termination is not iOS
background suspension. These runs use hidden Shadow imports, not real provider
or Console commands, and need no physical phone, provider credentials or
personal transcripts.

## Provider proofs and cleanup

A hidden `launch_surface` keeps a session off the default timeline but is not a
retirement receipt. Before sealing evidence, archive and user-hide each exact QA
session, verify its ID is absent from the served inventory, stop owned
processes, close tabs and simulators, and remove scratch homes. Do not make
cleanup depend on a downstream archive, backup or ingest worker, which may be
degraded while the proof stays valid.

Live proofs run only in a separately provisioned worker with its own isolated
environment, deliberately supplied credentials and private inputs. They are not
commands for a developer login. Fixture lanes never use real provider
credentials. Live proofs need an explicit image:

```bash
python3 scripts/qa/test-isolation.py \
  --live --image IMAGE --credentials PRIVATE_CREDENTIALS.json \
  --target test-terminal-fidelity-web \
  --command make test-terminal-fidelity-web \
  FIDELITY_CASES=/work/authorized/cases.json \
  PLAYWRIGHT_BASE_URL=https://authorized-runtime.example
```

`--credentials FILE` is a private JSON file with only the supported test
credentials. Public settings use `--set KEY=VALUE`. Neither lane imports the
operator's environment.

### Terminal fidelity

The public dispatcher runs the web proof only. It refuses
`test-terminal-fidelity-ios` and `test-mobile-chat-replay`, which take private
input and need a separately authorized disposable macOS worker that owns the
server URL, token, source path and simulator. Never upload personal transcripts
or tokens in workflow inputs.

The case manifest is a JSON array of `{ "name", "session_id", "markers" }`
objects. Markers are exact, distinct, whitespace-free final assistant replies,
not text copied from a prompt. iOS also requires `source_path` and checks that
its SHA-256 stays unchanged. Use only hidden test sessions. A scratch Runtime
Host with auth disabled is also valid; the browser proof never logs in to an
arbitrary protected hosted URL.

The checks use real served data, not API mocks. They keep ordered-reply,
cold-open and return, screenshot and timing evidence under
`artifacts/terminal-fidelity/`. iOS also verifies painted final text and
termination and reopen. Prefer short, distinct natural-word replies for optical
checks, and keep a failed provider result visible even when others pass.

There is no single local command for the whole campaign. An authorized worker
may compose web, iOS and simlab stages with an explicit provider list and
`--token-env NAME`, retaining failures and all evidence under a unique
`artifacts/terminal-fidelity/gate-*` directory. It is an operator
qualification, not a gate on every push. These checks complement simlab's
connection-recovery scenarios, and neither proves cellular behavior.

To qualify a published release instead of the working tree, install it into a
clean environment and add `--require-released-build` to the gate. That refuses
anything but a published release across Runtime Host, CLI and engine. A build
counts as released only when the remote's `refs/tags/v<version>` names its
commit, because a `channel` field or local tag can be asserted by a working
tree. The check needs network access and refuses when the remote is unreachable.
Each summary ends with a receipt naming build identities, provider readiness and
verdicts, the failed boundary, the retained evidence path and the next
supported command. No proof records a provider version, so the receipt reports
readiness only.

### Remote installer upgrade check

Launch Gate upgrades from the previous stable release into the selected release.
In the native dispatcher set `LONGHOUSE_NATIVE_SMOKE_REMOTE=1`, the exact
expected version and commit, and `LONGHOUSE_NATIVE_SMOKE_PREVIOUS_TAG`. Unset,
it runs the local-build smoke. `make test-install` routes both through the
hosted native worker. The smoke verifies both distributed binaries and keeps
prior enrollment, credential permissions and native hook state in one
disposable HOME. It never loads or stops the user's service, and it proves
installation, not real provider execution.

## Shipper benchmark

The hosted benchmark (`make hosted-shipper-mixed-bench`) accepts
`HOSTED_SHIPPER_BENCH_FILES`, `HOSTED_SHIPPER_BENCH_EVENTS_PER_FILE`,
`HOSTED_SHIPPER_BENCH_BYTES_PER_EVENT` and `HOSTED_SHIPPER_BENCH_LIVE_COUNT` for
larger repeated workloads. Keep the latency budget unchanged and keep every run,
including failures. Upload receipts do not prove derived-index convergence; see
[Runtime data upgrades](runtime-data-upgrades.md).
