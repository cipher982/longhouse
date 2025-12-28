# Documentation

## For Developers

| File                               | Purpose                                                       |
| ---------------------------------- | ------------------------------------------------------------- |
| [DEVELOPMENT.md](./DEVELOPMENT.md) | Local dev setup, commands, troubleshooting                    |
| [DEPLOYMENT.md](./DEPLOYMENT.md)   | Production deployment guide                                   |
| [../AGENTS.md](../AGENTS.md)       | **Start here** - Project overview, architecture, key commands |

## Architecture

Current architecture spec (v2.2): **[specs/durable-runs-v2.2.md](./specs/durable-runs-v2.2.md)**

Previous (v2.1): [specs/jarvis-supervisor-unification-v2.1.md](./specs/jarvis-supervisor-unification-v2.1.md)

Historical (superseded v2.0): [archive/super-siri-architecture.md](./archive/super-siri-architecture.md)

## Directories

| Directory    | What's in it             | Lifecycle                        |
| ------------ | ------------------------ | -------------------------------- |
| `specs/`     | Architecture & design    | Permanent (evolves slowly)       |
| `work/`      | Active PRDs & task docs  | Temporary - move/delete when done |
| `completed/` | Implemented feature docs | Historical reference             |
| `archive/`   | Obsolete/superseded      | Historical reference             |
| `research/`  | Research notes           | Reference                        |

> **Note:** `completed/` and `archive/` may contain outdated commands and architecture snapshots. For current “how to run this repo”, start at `../AGENTS.md` and `DEVELOPMENT.md`.
