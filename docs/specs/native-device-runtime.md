# Native Device Runtime

The installed Longhouse device product is two paired Rust binaries:

- `longhouse` is the public device command.
- `longhouse-engine` owns the local Machine Agent, provider bridges, and health evidence.

The Runtime Host is a separate Python service named `longhouse-server`. It runs
where the web application and SQLite database live. The device installer never
installs Python, `uv`, or a server command.

## Device contract

`longhouse auth --url <runtime-url>` opens browser pairing and writes protected
machine state. `longhouse machine repair --repair-service` writes and starts the
paired Machine Agent service. `longhouse local-health --fast --json` provides
the Desktop health snapshot.

Provider binaries remain user-owned. The native facade supports Claude, Codex,
and OpenCode. A provider is either present as a complete native surface or is
not offered; it is never routed through a second implementation.

## Installer contract

The installer downloads or receives a verified paired `longhouse` and
`longhouse-engine`, activates them together, and refuses to overwrite a
different executable at `~/.local/bin/longhouse`. Reinstallation replaces a
verified native pair atomically.

## Verification

The hermetic installer smoke starts from a fresh home and traps `python`,
`python3`, `uv`, and `pip`. It installs the paired binaries, pairs against a
local Runtime Host fixture, writes the Machine Agent service, reads native
health, and verifies reinstallation. Provider tests use fake upstream binaries
to prove the supported native command contracts. The universal release smoke
adds transcript projection and Runtime Host ingestion.
