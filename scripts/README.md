# scripts

Entry points behind Make targets. Run them through `make`; the target names are
the interface. The exceptions are `ops/bench.sh` (iOS bench Mac) and
`ops/crunch.sh` (Linux build VM), which run a command on remote compute and are
called directly. Where generated files come from: the Code map in
[`ARCHITECTURE.md`](../ARCHITECTURE.md#code-map).

| Folder | What |
| --- | --- |
| `build/` | Build identity, cargo wrapper, iOS project stamp |
| `ci/` | CI helpers and repo guards run by `make validate` |
| `dev/` | Local dev servers (`make dev`, `make dev-demo`) |
| `generate/` | Code and artifact generators |
| `ui/`, `ui-fixtures/` | Web frame capture (`make ui-capture`) and its fixtures |
| `qa/`, `canary/`, `tests/` | Live QA, canaries, tests for these scripts |
| `ops/`, `release/` | Ship and promotion (rings, review gate, launch readiness), release and TestFlight orchestration, remote compute (`bench.sh`, `crunch.sh`), simulator and phone lanes, macOS release packaging |

`install.sh` is the public installer served at `get.longhouse.ai`.
