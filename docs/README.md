# Documentation

## For Developers

| File                               | Purpose                                                       |
| ---------------------------------- | ------------------------------------------------------------- |
| [DEVELOPMENT.md](./DEVELOPMENT.md) | Local dev setup, commands, troubleshooting                    |
| [DEPLOYMENT.md](./DEPLOYMENT.md)   | Production deployment guide                                   |
| [../AGENTS.md](../AGENTS.md)       | **Start here** - Project overview, architecture, key commands |

## Architecture

The v2.0 architecture spec: **[specs/super-siri-architecture.md](./specs/super-siri-architecture.md)**

## Directories

| Directory    | What's in it             | Lifecycle                        |
| ------------ | ------------------------ | -------------------------------- |
| `specs/`     | Architecture & design    | Permanent (evolves slowly)       |
| `work/`      | Active PRDs & task docs  | **Temporary** - delete when done |
| `completed/` | Implemented feature docs | Archive                          |
| `archive/`   | Obsolete/superseded      | Archive                          |
| `research/`  | Research notes           | Reference                        |

> **Note:** `work/` should be empty when no features are in flight. PRDs get deleted (not moved) once implemented.
