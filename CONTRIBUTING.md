# Contributing to Longhouse

Longhouse is the Apache-2.0 open core of a product for finding and steering CLI
coding-agent sessions on machines you own. This guide takes you from clone to a
passing change. Read [`ARCHITECTURE.md`](ARCHITECTURE.md) first for the system
map and vocabulary.

## Scope

Contributions should strengthen the public core: session ingest, timeline,
search and recall, managed local control, the machine APIs, self-hosting,
install and repair, and the client surfaces over those contracts.

Hosted signup, billing, provisioning and fleet operations live in a separate
proprietary control plane. Do not add them here. [`EDITIONS.md`](EDITIONS.md)
draws the boundary.

## Setup

Needs Python 3.12+ with [`uv`](https://docs.astral.sh/uv/), [`bun`](https://bun.sh)
for the web frontend, and a Rust toolchain if you touch the engine.

```bash
git clone https://github.com/cipher982/longhouse.git
cd longhouse
make dev        # web UI with hot reload at http://localhost:47200
make dev-demo   # disposable local Runtime Host, auth off, seeded demo data
```

`make dev` is interactive and uses the Runtime Host and device identity already
set by `longhouse auth`.

## Code layout

The [Code map in `ARCHITECTURE.md`](ARCHITECTURE.md#code-map) lists every
top-level directory, the web and iOS feature folders, the server service
families, where each generated file comes from, and where to look for common
topics. `web/`, `ios/`, `engine/` and `scripts/` each have a short README.

## Tests

Run the tier that matches your change.

| Change in | Run |
| --- | --- |
| `server/zerg/` (backend) | `make test` |
| `web/` (frontend) | `make test-frontend` |
| `engine/` (Rust agent) | `make test-engine` |
| `runner/` | `make test-runner` |
| UI or runtime behavior | `make test-e2e` |
| Any change, before pushing | `make validate`, then `make affected-check BASE=origin/main` and the focused proof it lists |

Backend tests live in `server/tests_lite/`: one SQLite database per test, no
shared conftest. Test targets run in a disposable container (Docker required)
with no host mounts, credentials or external network, and leave receipts under
`artifacts/test-isolation/<run-id>/`. Never give provider credentials to fixture
targets. iOS, macOS packaging and installer tests run only on a native runner
for a pushed SHA. How that isolation works, how to dispatch native and live
proofs, and what evidence they leave: [`docs/runbooks/maintainer-testing.md`](docs/runbooks/maintainer-testing.md).

## Generated code

Do not edit `server/zerg/generated/`, `web/src/generated/` or
`ios/Sources/Shared/Generated/` by hand. The
[Generated files table](ARCHITECTURE.md#generated-files) names each output's
source and generator. The two you will use most:

- WebSocket contract: edit `schemas/ws-protocol-asyncapi.yml`, then `make regen-ws`.
- HTTP routes or response models: `make generate-sdk`.

`make validate` fails when a generated file is stale.

## CI

A pull request runs unit tests and quality checks for backend, frontend and
engine. Those gate a normal PR, and a red one is yours to fix. Deploy, hosted QA
and image-build lanes are operational and do not block your change.

Three shapes are deliberate, so they are not missing gates:

- `make validate` runs on every push and PR, ungated, because it is
  cross-cutting. The live Runtime Host lifecycle proof beside it starts a real
  Runtime Host and runs when `server/`, `engine/`, `schemas/`, `scripts/` or CI
  plumbing change.
- A runtime deploy waits only on the suites that can invalidate it: backend,
  engine, frontend and runner, provider contract. Browser E2E, accessibility,
  model smoke and onboarding smoke run after the fact, and hosted live QA is
  dispatched asynchronously.
- Main runs queue and branch or PR runs cancel, because a main run is a deploy
  candidate whose receipt must not vanish.

## Pull requests

- Keep commits atomic and the change focused.
- Match the surrounding style and naming.
- Call out any new DB column, env var or schema change.
- Say plainly what is tested and what is not.

Good first issues carry the [`good first issue`](https://github.com/cipher982/longhouse/labels/good%20first%20issue)
label. Well-isolated entry points are the web timeline UI, more provider-CLI
ingest parsers, CLI subcommand UX, and docs.

By contributing you agree your contributions are licensed under Apache-2.0.
