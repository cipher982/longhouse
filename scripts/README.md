# scripts

Entry points behind Make targets. Run them through `make`; the target names are
the interface. Where generated files come from: the Code map in
[`ARCHITECTURE.md`](../ARCHITECTURE.md#code-map).

| Folder | What |
| --- | --- |
| `build/` | Build identity, cargo wrapper, iOS project stamp |
| `ci/` | CI helpers and repo guards run by `make validate` |
| `dev/` | Local dev servers (`make dev`, `make dev-demo`) |
| `generate/` | Code and artifact generators |
| `ui/`, `ui-fixtures/` | Web frame capture (`make ui-capture`) and its fixtures |
| `qa/`, `canary/`, `tests/` | Live QA, canaries, tests for these scripts |
| `ops/`, `release/` | Deploy, ship monitoring, release packaging |

`install.sh` is the public installer served at `get.longhouse.ai`.
