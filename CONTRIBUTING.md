# Contributing to Longhouse

Thanks for taking a look. Longhouse is the Apache-2.0 open core of a product
for finding and steering CLI coding-agent sessions on machines you own. This
guide gets you from clone to a passing change.

New to the codebase? Read [`ARCHITECTURE.md`](ARCHITECTURE.md) first — it has
the system map and a glossary of the project's nouns.

## Scope

Contributions should strengthen the public core: session ingest, the timeline,
search and recall, managed local control, the machine APIs, self-hosting,
install/repair, and the client surfaces over those contracts.

Hosted signup, billing, provisioning, and fleet operations are **not** part of
this repository — they live in a separate proprietary control plane. Please
don't add them here. See [`EDITIONS.md`](EDITIONS.md) for the boundary.

## Dev setup

Prerequisites: a recent **Python 3.12+** with [`uv`](https://docs.astral.sh/uv/),
[`bun`](https://bun.sh) for the web frontend, and a **Rust** toolchain if you
touch the engine.

```bash
git clone https://github.com/cipher982/longhouse.git
cd longhouse
make dev        # local web UI with hot reload against your linked Runtime Host
make dev-demo   # isolated local backend + seeded demo UI
```

`make dev` is interactive and serves the local web source at
`http://localhost:47200`, using the Runtime Host and device identity already
configured by `longhouse auth`. `make dev-demo` runs a disposable local Runtime
Host with authentication disabled.

## Project layout

```
server/    Python: FastAPI Runtime Host, CLI, SQLite-backed state
web/        TypeScript/React frontend (bundled into the Runtime Host)
engine/     Rust Machine Agent (longhouse-engine) — ships session events
runner/     Rust optional WebSocket command executor
ios/        SwiftUI read/steer client
schemas/    Source-of-truth contracts (e.g. ws-protocol-asyncapi.yml) for generated code
docs/       Specs and runbooks — see docs/README.md for an index
```

## Tests

Run the tier that matches your change — don't over-test:

| Change in                | Run                  |
| ------------------------ | -------------------- |
| `server/zerg/` (backend) | `make test`          |
| `web/` (frontend)        | `make test-frontend` |
| `engine/` (Rust agent)   | `make test-engine`   |
| `runner/`                | `make test-runner`   |
| UI / runtime behavior    | `make test-e2e`      |
| Before pushing           | `make test-ci`       |

Ordinary `test-*`, `validate-*`, `qa-*`, and onboarding-funnel targets run
through the disposable portable test boundary. Local runs require Docker; the
supervisor copies the current source into a container with no host mounts,
ambient credentials, or external network access. Dependency preparation has
network access and receives only the package manifests. Browser binaries come
from the official Playwright image pinned to the version in `bun.lock`.
Receipts and collected evidence go under `artifacts/test-isolation/<run-id>/`.
Each run has its own container and private home. The container deadline survives
a killed supervisor; the next run removes expired resources owned by that
checkout without touching active runs or another checkout's resources.
Do not provide provider credentials to fixture targets.

Backend tests go in `server/tests_lite/` (per-test SQLite DBs, no shared
conftest). For `ios/` changes, use the native CI lane described below.

### Native test isolation

Native iOS, macOS packaging, installer, and WebKit onboarding fixtures run
only for an exact pushed SHA on a fresh standard GitHub-hosted macOS runner.
CI invokes `python3 scripts/qa/native-test-bootstrap.py --target TARGET` with
explicit `NATIVE_OPTIONS_JSON`; it creates a disposable home and records
`$RUNNER_TEMP/native-isolation-evidence/`. There is no local native fallback and
no self-hosted Mac or paid-runner substitute. Inspect the one-day CI artifact
and its receipt when a native fixture fails.

Fixture lanes never use real provider credentials. Live proofs require an
explicit image through
`scripts/qa/test-isolation.py --live --image IMAGE --target TARGET`.
Authenticated proofs also require `--credentials FILE`, a private JSON file
containing only the explicitly supported test credentials. Public settings use
`--set KEY=VALUE`; neither lane imports the operator's environment.
Private-input native proofs (`test-mobile-chat-replay` and
`test-terminal-fidelity-ios`) are deliberately refused by public fixture CI.
They require a separately authorized disposable macOS worker; never upload
personal transcripts or tokens in workflow inputs.

### Session status and motion

The web session and local replay studio use the same Ledger renderer. Start
`make live-status-lab`, then run `make capture-ledger-journeys` to exercise the
production session route against isolated HTTP/SSE fixtures. This checks activity
expiry, a silent open connection, recovery, host/transcript uncertainty, approval,
draft retention, completion, narrow layouts, and reduced motion without issuing
provider commands.

For recorded transcript content, use `make capture-live-status SESSION=<id>`,
then pass `CAPTURE=<private.json>` to the journey target or to
`make capture-live-status-frames SURFACE=ledger`. Captures contain private session
content; keep the generated frames, videos, and manifests local and untracked.
Inspect the frames as well as the assertions, then stop the owned lab process.
For native status layouts, use the native CI dispatcher rather than executing
on the developer Mac:

```bash
make ios-previews
make ios-ui-shot TEST=SessionChatUITests/testKeyboardFocusKeepsLatestTranscriptMessageVisible
```

### Native pipeline recovery

`simlab-run` dispatches the clean pushed-SHA native job off the laptop; it
does not run native code against the developer login. The helper assertions
remain portable:

```bash
make simlab-run SCENARIOS="interrupted-client-recovery client-network-recovery"
make test-ios-helper
```

Each verdict requires the final synthetic source reply in the server projection
and a matching client-render acknowledgement; server-only progress cannot pass.
Screenshots, logs, projections, timings, and verdicts are retained in the native
CI artifact and downloaded beneath the run's `artifacts/test-isolation/` directory.
Inspect the screenshots as well as the verdict. The loopback relay models
connection loss, not cellular hardware; app termination is not iOS background
suspension. These are hidden Shadow imports, not real provider/Console command
tests. No physical phone, provider credentials, or personal transcripts are used.

Provider proofs own cleanup end to end. A hidden `launch_surface` keeps a
session out of the default timeline, but it is not a deletion/retirement
receipt: before sealing evidence, archive and user-hide each exact QA session,
verify its ID is absent from the served inventory, stop owned processes, close
tabs/simulators, and remove scratch homes. Do not make proof cleanup depend on a
downstream archive, backup, or ingest worker; those paths may be degraded while
the proof remains valid.
The operator commands below run only inside that separately provisioned worker,
with its own isolated test environment and deliberately supplied credentials and
private inputs. They are not commands to run under the developer's login.

For real provider sessions, create the hidden proof sessions with the
operator's authorized tooling, then dispatch the web proof explicitly through
the live portable lane. The image, credential JSON, and public settings are
required inputs; nothing is inferred from the developer's environment:

```bash
python3 scripts/qa/test-isolation.py \
  --live --image IMAGE --credentials PRIVATE_CREDENTIALS.json \
  --target test-terminal-fidelity-web \
  --command make test-terminal-fidelity-web \
  FIDELITY_CASES=/work/authorized/cases.json \
  PLAYWRIGHT_BASE_URL=https://authorized-runtime.example
```

The public dispatcher deliberately refuses `test-terminal-fidelity-ios`.
That private-input native proof is available only to a separately authorized,
disposable macOS worker whose owner provisions the private manifest, token, and
simulator; it is not a `make` command for a developer checkout and must never
run under the developer's login.

The JSON manifest is an array of `{ "name": "...", "session_id": "...",
"markers": ["SUMMIT_BLUE", "RIVER_GREEN"] }` objects. Markers are exact, distinct,
whitespace-free final assistant replies, not text copied verbatim into a prompt.
iOS additionally requires `source_path` and verifies its SHA-256 stays unchanged.
Use only hidden/test sessions. The web dispatcher receives credentials solely
from `PRIVATE_CREDENTIALS.json` and receives its explicit runtime URL and case
manifest through the authorized command. A scratch Runtime Host with auth
disabled is also valid; the browser proof never silently logs in to an
arbitrary protected hosted URL. The separately authorized native worker owns
the iOS server URL, token, source path, and simulator destination.
These checks use real served data, not API mocks. They retain ordered-reply,
cold-open/return, screenshots, and timing evidence under
`artifacts/terminal-fidelity/`. iOS additionally verifies painted final text
and termination/reopen. Prefer short, distinct natural-word replies for optical
checks; long machine identifiers can wrap ambiguously. A provider matrix failure
must stay visible even when other providers pass. These checks complement
simlab's connection-recovery scenarios; neither proves cellular-radio behavior.
There is no complete local terminal-fidelity campaign command. The public
dispatcher can run the web proof only; it refuses the private native iOS proof.
The separately authorized disposable worker may compose web, iOS, and simlab
stages with an explicit provider matrix and `--token-env NAME`, retaining
failures and all evidence under a unique
`artifacts/terminal-fidelity/gate-*` directory. This is an operator
qualification, not a credential-dependent gate on every push.

Every run records what it exercised. A dogfood build and an installed release
pass the same stages, so a green run says nothing on its own about the binaries
a person can download. The authorized worker may add
`--require-released-build` to its gate invocation to refuse anything but a
published release across the Runtime Host, CLI, and engine; that qualification
is not available from a developer checkout.

Use it after installing a release into a clean environment, to qualify the
release itself rather than the working tree. A build counts as released only
when the remote's own `refs/tags/v<version>` names its commit; both the
`channel` field and a local tag are things a working tree can assert about
itself. The check needs network access and refuses when it cannot reach the
remote. Each summary ends with a receipt naming the
product build identities, provider readiness and verdicts, which boundary
failed, the retained evidence path, and the next supported command for that
boundary. No current proof records a provider _version_, so the receipt reports
readiness rather than claiming a provider build it does not have.

Historical indexing is a separate qualification from upload receipts. On the
Runtime Host, supply a JSON array of session UUIDs (or objects with `session_id`):

```bash
python -m zerg.cli.historical_convergence \
  --catalog-db /data/longhouse-live.db --search-db /data/search.db \
  --cohort /data/cache/cohort.json --output /data/cache/convergence.json \
  --wait --timeout 600
```

The checker opens both databases read-only, captures fixed target revisions,
and verifies published search fences and active embedding completion.
`--wait` fails on incomplete convergence or timeout; without it, a zero-exit
`pending` result is only a diagnostic snapshot, not a pass. It retains counts
and failure categories, never transcript text. The checkout equivalent is
`make historical-convergence-check ARGS="..."`.

The existing hosted benchmark accepts `HOSTED_SHIPPER_BENCH_FILES`,
`HOSTED_SHIPPER_BENCH_EVENTS_PER_FILE`, `HOSTED_SHIPPER_BENCH_BYTES_PER_EVENT`,
and `HOSTED_SHIPPER_BENCH_LIVE_COUNT` for larger repeated workloads. Keep the
latency budget unchanged and retain each run, including failures; don't call
upload receipts proof of derived-index convergence.

Launch Gate's remote installer checks upgrade from the preceding stable public
release into the selected release. In the authorized native dispatcher, set
`LONGHOUSE_NATIVE_SMOKE_REMOTE=1`, the exact expected version/commit, and
`LONGHOUSE_NATIVE_SMOKE_PREVIOUS_TAG`; the worker owns its evidence directory.
The default unset mode remains the local-build smoke, and `make test-install`
routes both modes through the hosted native worker.
It verifies both distributed binaries and preserves prior enrollment,
credential permissions, and native hook state in one disposable HOME.
The app stays in that HOME's Applications directory; the smoke never loads
or stops the user's service. Its API/provider fixtures prove installation,
not real provider execution or client viewing; use the fidelity campaign
for those obligations.

## Runtime data upgrades

Existing render history needs an explicit branch-count backfill when upgrading
to generation-wide abandoned-output counts. Prepare a verified cache before
the API cutover; apply it once the updated catalog writer is running:

```bash
longhouse-server db repair-render-counts --database /data/longhouse-live.db --cache /data/cache/render-counts.jsonl
longhouse-server db repair-render-counts --database /data/longhouse-live.db --cache /data/cache/render-counts.jsonl --apply
```

Use the actual catalog database and immutable-object root for your installation
(`--object-root` overrides the Runtime Host setting). Preparation reads sealed
render files, not raw provider archives. Applying uses the catalog writer and
requires zero missing current facts; neither phase rewrites source history.
Keep the cache across container replacement. `--limit` samples preparation
only and cannot be combined with `--apply`.

Catalog schema fingerprints reject mixed reader/writer versions. Use an explicit
maintenance window when the writer cannot be upgraded independently: start the
new catalog, apply the prepared facts, then start the new API. Repeat `--apply`
after the API upgrade to cover any final objects created by the old writer.

## Generated code

Some code is generated — **do not edit it by hand**:

- `server/zerg/generated/`, `web/src/generated/`,
  `ios/Sources/Shared/Generated/`

To change the WebSocket contract, edit `schemas/ws-protocol-asyncapi.yml` and
run `make regen-ws`. After changing HTTP routes or response models, run
`make generate-sdk`. `make validate` checks every contract for drift.

## CI

Opening a PR triggers a matrix of checks. The ones that gate a normal PR are
the backend/frontend/engine unit tests and quality/lint; the rest
(deploy, hosted QA, image builds) are operational lanes that won't block your
contribution. A red unit-test job is yours to fix; a red deploy/hosted lane
usually isn't.

## Pull requests

- Keep commits atomic and the change focused.
- Match the surrounding code's style and naming.
- If you add a DB column, env var, or touch schema, call it out in the PR.
- Be honest in the PR description about what's tested and what isn't.

## Good first issues

Look for the [`good first issue`](https://github.com/cipher982/longhouse/labels/good%20first%20issue)
label. Well-isolated entry points include the web timeline UI, additional
provider-CLI ingest parsers, CLI subcommand UX, and docs.

By contributing you agree your contributions are licensed under Apache-2.0.
