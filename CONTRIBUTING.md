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
make dev        # backend + bundled web UI with hot reload
```

`make dev` is interactive and runs the Runtime Host plus the web UI. Auth is
disabled in dev by default. The web UI lives at `http://localhost:8080`.

## Project layout

```
server/    Python: FastAPI Runtime Host, CLI, SQLite-backed state
web/        TypeScript/React frontend (bundled into the Runtime Host)
engine/     Rust Machine Agent (longhouse-engine) — ships session events
runner/     Rust optional WebSocket command executor
ios/        SwiftUI read/steer client
schemas/    Source-of-truth contracts (e.g. tools.yml) for generated code
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

## Generated code

Some code is generated — **do not edit it by hand**:

- `server/zerg/generated/`, `server/zerg/tools/generated/`, `web/src/generated/`

To change tool contracts, edit `schemas/tools.yml` and run
`make generate-tools`.

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
