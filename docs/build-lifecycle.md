# Build lifecycle and cache policy

## Problem

The Rust engine currently allows generated build state to grow without a
bounded lifecycle. The current checkout accumulated approximately 38 GB under
`engine/target`, including 26 GB of debug dependencies and 9.8 GB of Rust
incremental state. The target contains debug, release, and CI profiles, and
the debug incremental directory contains dozens of historical fingerprints.

The immediate trigger is build identity generation. The generator writes a
new `built_at` timestamp on every invocation. `engine/build.rs` watches the
identity file and exports every field as a compile-time environment variable,
including `LONGHOUSE_BUILD_BUILT_AT`. The Makefile invokes the generator before
many build and test commands. Repeated invocations therefore invalidate the
engine even when source, commit, and build mode have not changed, leaving old
incremental fingerprints behind.

## Goals

1. Repeated local builds with unchanged source state must not create a new
   compile identity or a new incremental fingerprint.
2. Release builds must retain an accurate, stable build timestamp for the
   complete release operation.
3. Local, CI, release, and worktree build outputs must be isolated and
   attributable to one cache root.
4. Build storage must have a visible budget and an explicit cleanup path.
5. Existing version, provenance, health, and packaging output must remain
   compatible.

## Non-goals

- Do not split the engine into a multi-crate workspace as part of this fix.
- Do not remove dependencies solely to reduce compiler output.
- Do not silently delete a user's build cache during an ordinary build.
- Do not make CI depend on a developer machine's cache volume.

## Design

### 1. Stable build identity generation

The identity generator keeps the existing JSON schema, but becomes
content-stable and serialized across processes:

- `version`, `commit`, `commit_short`, `dirty`, and `channel` remain semantic
  identity fields.
- In development mode, `built_at` is preserved when the semantic identity is
  unchanged. It changes only when the version, commit, dirty state, or channel
  changes.
- Release jobs may provide `LONGHOUSE_BUILD_TIMESTAMP`. When they do not, a
  tagged release derives the timestamp from the tagged commit's committer
  timestamp, making separate architecture jobs deterministic. Manual release
  callers may provide an explicit timestamp.
- The generator compares serialized bytes before writing either staged copy.
  Unchanged identity files retain their mtime.
- A lock under `.build/locks/` covers reading the previous identity, choosing
  the timestamp, serializing, and replacing both staged copies. This prevents
  concurrent generators from racing to create different timestamps.
- Writes are atomic: write a sibling temporary file, fsync/replace it, and
  leave the destination untouched when bytes are unchanged.

`build.rs` continues to validate freshness and emit the compile-time identity,
but a no-op identity generation must remain a Cargo no-op.

### 2. One build entrypoint and artifact contract

All repository-owned Rust build commands go through one small wrapper. The
wrapper:

- finds the repository/worktree root;
- uses a target path scoped to that checkout, with a lock key derived from its
  absolute target path;
- sets `CARGO_TARGET_DIR` under the configured cache root;
- invokes Cargo without changing arguments;
- runs a bounded preflight/postflight health check.

The default target is `.build/cargo-target` in the repository. The target root
is configurable with `LONGHOUSE_CARGO_TARGET_DIR`; CI overrides it with a
job-local temporary directory. An existing standard `CARGO_TARGET_DIR` is
honored when explicitly set by the caller. No checked-in file contains a
developer-specific absolute path.

The target layout is:

```text
<target-dir>/
  debug/
  release/
  ci/
```

The wrapper must be used by the Makefile and repository scripts. It exposes a
machine-readable artifact resolver (`artifact --profile <name> --bin <name>`)
and every consumer that needs a binary uses that resolver rather than
reconstructing `engine/target/...`. CI may call Cargo directly only when it
explicitly sets its disposable target directory and uses the same resolver.

The wrapper holds a per-target lock from preflight through Cargo and
postflight. Locks live under `.build/locks/`, outside the directory that cleanup
may rename or delete. Cleanup canonicalizes paths, rejects symlinks, requires a
Longhouse target marker, renames the target to a uniquely named sibling under
the lock, and deletes the renamed directory.

### 3. Profile policy

Local development keeps incremental compilation, but uses reduced debug
information sufficient for ordinary debugging. CI and release builds disable
incremental compilation and use the existing release/CI optimization policy.

The profile policy is:

- `dev`: incremental on, `debug = 1`.
- `test`: explicitly configured with the same debug/incremental policy as local
  development; it does not rely on Cargo's implicit profile inheritance.
- `release`: incremental off, stripped release output as currently configured.
- `ci`: incremental off, no debug information, no LTO as currently configured.

The build wrapper exposes `LONGHOUSE_CARGO_DEBUG=full` as an escape hatch for a
full-debug local build without changing the repository defaults.

### 4. Cache health and retention

The repository gains an explicit build-health command that reports:

- target path and profile sizes;
- number and age of incremental directories;
- total target size;
- whether the cache exceeds its budget.

The default local budget is 12 GB per target. The wrapper refuses to start a
new build when the existing target exceeds the budget and prints the exact
cleanup command. After a successful build it reports if the target crossed the
budget; the preflight admission check is the hard guard because a build can
grow while it runs. It does not silently remove files. A cleanup command
removes only generated target output for the selected target. CI cleanup is
automatic because CI uses a disposable target directory.

The health check must not recursively scan the entire home directory. It only
inspects the selected target root and uses a lock so concurrent builds cannot
clean or inspect the same target while Cargo is running.

### 5. Build identity and packaging boundaries

Development builds may display commit/channel/dirty provenance and retain the
JSON `built_at` field, but timestamp changes must not be used as a heartbeat or
per-invocation build marker. Release packaging creates one identity, builds all
release artifacts from it, and stages that same identity into Python, Rust, and
iOS outputs.

## Verification

After implementation and cleanup:

1. Generate identity twice without changing source state; bytes and mtimes must
   remain unchanged on the second invocation.
2. Run concurrent identity generation; both staged copies must converge to one
   identity and Cargo must observe one final mtime.
3. Build twice through the wrapper; the second build must not create a new
   identity fingerprint solely because time advanced.
4. Run the engine test path twice; target growth must remain within the local
   budget and incremental directory count must remain stable.
5. Verify artifact resolution works for debug, release, CI, and an explicit
   target triple.
6. Verify release/CI commands still produce both binaries and preserve the
   expected build identity fields.
7. Verify cleanup refuses unmarked/symlinked paths and succeeds for the marked
   target while a second cleanup/build is locked out.
8. Verify the old in-repository `engine/target` is absent after cleanup and is
   recreated only if a developer explicitly bypasses the wrapper.

## Cleanup order

The existing generated target is cleaned only after the identity and wrapper
changes are in place. This prevents a clean rebuild from immediately
recreating the same unbounded state.
