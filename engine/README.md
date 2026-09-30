# engine

The Rust Machine Agent. It builds two binaries: `longhouse-engine`
(`src/main.rs`: discovers provider transcripts, ships them with a spool and
retry, heartbeats, runs the control channel) and the native `longhouse` CLI
(`src/longhouse.rs`: auth, local health, machine repair, and managed provider
launches such as `longhouse codex`). Where it sits in the
system: the Code map in [`ARCHITECTURE.md`](../ARCHITECTURE.md#code-map).

- `src/pipeline/` parses and compresses transcripts; `src/shipping/` sends them.
- `src/state/` is the local SQLite state: spool, file offsets, session phase.
- `src/import_scope.rs` is what local history the machine may ship (a start
  time plus optional project folders, `machine/import-scope.json`). The engine
  enforces it where a source enters shipping (`discovery.rs`, the OpenCode
  session walk, the Claude presence hook; Cursor conversations are judged by
  their own start and folder in `cursor_store.rs`); `machine_scope.rs` is the
  `longhouse machine scope` command that writes it and `machine_uninstall.rs`
  is `longhouse uninstall` and the device-token revocation `auth --clear` uses.
- Provider modules are named by provider (`codex_*`, `claude_*`, `cursor_*`,
  `antigravity_*`); `managed_*` is the provider-neutral managed-session layer.
- `src/managed_phase_contract.rs` and `src/managed_identity_contract.rs` are
  generated; never edit them.

Test: `make test-engine`. Install locally: `make install-engine`.
