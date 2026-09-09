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

| Change in | Run |
|-----------|-----|
| `server/zerg/` (backend) | `make test` |
| `web/` (frontend) | `make test-frontend` |
| `engine/` (Rust agent) | `make test-engine` |
| `runner/` | `make test-runner` |
| UI / runtime behavior | `make test-e2e` |
| Before pushing | `make test-ci` |

Backend tests go in `server/tests_lite/` (per-test SQLite DBs, no shared
conftest). For `ios/` changes, run the Xcode `Longhouse` scheme tests.

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
For native status layouts use `make ios-previews`; `make ios-ui-shot
TEST=SessionChatUITests/testKeyboardFocusKeepsLatestTranscriptMessageVisible`
also captures the real keyboard and composer.

### Native pipeline recovery

On macOS with Xcode and an iOS Simulator installed, `make simlab-run` exercises
the real app against a scratch Runtime Host and Machine Agent. It covers live
transcript arrival, malformed/split input, abandoned sends, app termination and
reopen, and network loss/reconnection without relaunch. To run only recovery:

```bash
make simlab-run SCENARIOS="interrupted-client-recovery client-network-recovery"
make test-ios-helper
```

Each verdict requires the final synthetic source reply in the server projection
and a matching client-render acknowledgement; server-only progress cannot pass.
Screenshots, logs, projections, timings, and failure verdicts are retained under
the unique scratch run linked from `artifacts/simlab/current/summary.json`.
Inspect the screenshots as well as the verdict. The loopback relay models
connection loss, not cellular hardware; app termination is not iOS background
suspension. These are hidden Shadow imports, not real provider/Console command
tests. No physical phone, provider credentials, or personal transcripts are used.

For real provider sessions, use `make test-console-served-state-e2e ARGS="--help"`
to create explicit hidden proof sessions, then feed their actual assistant replies
to the real-client checks:

```bash
make test-terminal-fidelity-web FIDELITY_CASES=/tmp/cases.json PLAYWRIGHT_BASE_URL=http://127.0.0.1:47200
make test-terminal-fidelity-ios FIDELITY_CASES=/tmp/cases.json IOS_DESTINATION="platform=iOS Simulator,id=<uuid>"
```

The JSON manifest is an array of `{ "name": "...", "session_id": "...",
"markers": ["SUMMIT_BLUE", "RIVER_GREEN"] }` objects. Markers are exact, distinct,
whitespace-free final assistant replies, not text copied verbatim into a prompt.
iOS additionally requires `source_path` and verifies its SHA-256 stays unchanged.
Use only hidden/test sessions. Set the iOS target explicitly with
`LONGHOUSE_FIDELITY_SERVER_URL` and `LONGHOUSE_FIDELITY_AUTH_TOKEN`.
For web, start `make dev` first: its linked-runtime proxy supplies authentication.
Alternatively use a scratch Runtime Host with auth disabled. The browser proof
does not silently log in to an arbitrary protected hosted URL.
These checks use real served data, not API mocks. They retain ordered-reply,
cold-open/return, screenshots, and timing evidence under
`artifacts/terminal-fidelity/`. iOS additionally verifies painted final text
and termination/reopen. Prefer short, distinct natural-word replies for optical
checks; long machine identifiers can wrap ambiguously. A provider matrix failure
must stay visible even when other providers pass. These checks complement
simlab's connection-recovery scenarios; neither proves cellular-radio behavior.

To run the complete local campaign without assembling a case manifest by hand:

```bash
make test-terminal-fidelity-gate ARGS="--server-url https://your-runtime.example --browser-url http://127.0.0.1:47200 --device-id your-machine --provider codex --cwd /path/to/workspace --ios-destination 'platform=iOS Simulator,id=<uuid>'"
```

Run on the provider-owning Mac after `make dev`. Repeat `--provider` for an
explicit matrix. The command uses the existing machine token (or `--token-env
NAME`), binds each successful Console proof to its original native source,
runs web and iOS viewing, then both simlab recovery scenarios. Every requested
provider remains in the verdict, including failures; unavailable prerequisites
cannot silently skip a required stage. Logs, source hashes, individual proofs,
and the final summary stay in a unique `artifacts/terminal-fidelity/gate-*`
directory. This is an operator qualification, not a credential-dependent gate
on every push. `make test-terminal-fidelity-gate-helper` checks its failure
boundaries without a provider or simulator.

Every run records what it exercised. A dogfood build and an installed release
pass the same stages, so a green run says nothing on its own about the binaries
a person can download. Add `--require-released-build` to refuse anything but a
published release across the Runtime Host, CLI and engine:

```bash
make test-terminal-fidelity-gate ARGS="--require-released-build --server-url ... --device-id ... --provider codex --cwd ... --ios-destination '...'"
```

Use it after installing a release into a clean environment, to qualify the
release itself rather than the working tree. A build counts as released only
when the remote's own `refs/tags/v<version>` names its commit; both the
`channel` field and a local tag are things a working tree can assert about
itself. The check needs network access and refuses when it cannot reach the
remote. Each summary ends with a receipt naming the
product build identities, provider readiness and verdicts, which boundary
failed, the retained evidence path, and the next supported command for that
boundary. No current proof records a provider *version*, so the receipt reports
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
release into the selected release. For a manual run, set
`LONGHOUSE_NATIVE_SMOKE_REMOTE=1`, the exact expected version/commit,
`LONGHOUSE_NATIVE_SMOKE_PREVIOUS_TAG`, and
`LONGHOUSE_NATIVE_SMOKE_ARTIFACT_DIR`, then run `make test-install`.
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
