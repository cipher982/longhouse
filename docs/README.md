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
| `work/`      | Active PRDs & task docs  | **Temporary** - delete when done |
| `completed/` | Implemented feature docs | Archive                          |
| `archive/`   | Obsolete/superseded      | Archive                          |
| `research/`  | Research notes           | Reference                        |

> **Note:** `work/` should be empty when no features are in flight. PRDs are either moved to `completed/` for historical reference or deleted once implemented.
