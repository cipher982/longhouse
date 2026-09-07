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
make test-terminal-fidelity-web FIDELITY_CASES=/tmp/cases.json PLAYWRIGHT_BASE_URL=<runtime-url>
make test-terminal-fidelity-ios FIDELITY_CASES=/tmp/cases.json IOS_DESTINATION="platform=iOS Simulator,id=<uuid>"
```

The JSON manifest is an array of `{ "name": "...", "session_id": "...",
"markers": ["SUMMIT_BLUE", "RIVER_GREEN"] }` objects. Markers are exact, distinct,
whitespace-free final assistant replies, not text copied verbatim into a prompt.
iOS additionally requires `source_path` and verifies its SHA-256 stays unchanged.
Use only hidden/test sessions. Set the iOS target explicitly with
`LONGHOUSE_FIDELITY_SERVER_URL` and `LONGHOUSE_FIDELITY_AUTH_TOKEN`; browser
authentication follows the existing live Playwright configuration.
These checks use real served data, not API mocks. They retain ordered-reply,
cold-open/return, screenshots, and timing evidence under
`artifacts/terminal-fidelity/`. iOS additionally verifies painted final text
and termination/reopen. Prefer short, distinct natural-word replies for optical
checks; long machine identifiers can wrap ambiguously. A provider matrix failure
must stay visible even when other providers pass. These checks complement
simlab's connection-recovery scenarios; neither proves cellular-radio behavior.

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
