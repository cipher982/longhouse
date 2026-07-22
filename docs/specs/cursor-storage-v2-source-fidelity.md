# Cursor Storage-v2 Source Fidelity

Status: implemented for Cursor Shadow and Helm; Console remains separate
Date: 2026-07-13
Owner: Longhouse Machine Agent
Related:

- `VISION.md`
- `docs/specs/cursor-transcript-format.md`
- `docs/specs/capability-gated-degraded-helm.md`
- `docs/specs/session-identity-kernel.md`

## Decision

Cursor transcript capture moves entirely into the Rust Machine Agent and
storage-v2.  The archive contract is **source fidelity**, not merely a complete
render of the current known Cursor message shapes.

For every supported Cursor `store.db` source, Longhouse must durably retain
the exact bytes of every observed `meta` value and every observed `blobs` row,
with the provider-native conversation identity and blob provenance needed to
reconstruct the observed logical store later.  A parser may render only the
known subset today.  Unknown rows, snapshot fields, and message blocks remain
raw durable evidence with an explicit render gap; they are never dropped or
coerced into guessed canonical events.

This replaces all Cursor uses of `POST /api/agents/ingest`:

- `longhouse cursor import` (Shadow import),
- `cursor_helm_ingest.py` (interactive Helm tailer), and
- `cursor_acp.rs` direct `SessionIngest` posting (Console).

There is no v1 fallback after the cut.  A Cursor surface that cannot prove a
storage-v2 source is unavailable or control-only; it is never called durable.

## Product truth

Cursor is two independent axes:

| Surface | Control proof | Archive proof | Product state before the adapter |
| --- | --- | --- | --- |
| Shadow | none | native `store.db` storage-v2 source and renderer | durable, readable, observe-only |
| Helm | Cursor PTY + control socket + engine lease | hook/store identity claim onto the native source | managed, remotely controllable, durable, and readable |
| Console | native `cursor_print` turn adapter | native `store.db` identity and storage-v2 receipt binding | managed, durable, readable, one turn per stock `cursor-agent --print` invocation |

The Cursor provider contract advertises transcript and phase capabilities only
for sessions with the proven native source and observed binding claim.
Helm never originates from a remote client. Cursor Console is separately
represented by `session.turn.start` through the
native `cursor_print` adapter; it is not an ACP fallback and does not grant
Helm capabilities. Helm `send`, graceful Ctrl-C `interrupt`, native `resume`,
permission response, and explicit `terminate` remain separate capabilities.

## Source-fidelity contract

### Logical source identity

A Cursor logical source is identified by the provider-native
`conversationUuid` / `meta['0'].agentId`, after verifying they agree.  The
opaque source id is derived only from that normalized provider identity and
provider name.  Filesystem paths, workspace hash, title, creation time, and
"newest store after launch" are diagnostics, never identity or binding proof.

The adapter opens `store.db` read-only and WAL-aware.  It does not checkpoint,
copy over, delete, upload the database file, or modify Cursor state.

### Raw records

The raw lane is an append-only capture log with `range_kind=record_ordinal`.
Each captured item is a versioned `cursor_store_record/v1` byte payload.  Its
canonical fields include:

```text
v                    # 1
kind                 # meta | blob | root_observation
conversation_uuid
store_incarnation
meta_key             # for meta
meta_value_bytes_b64 # exact SQLite value bytes, for meta
meta_value_storage_class # text | blob, for meta
blob_id              # content-addressed id, for blob/root_observation
blob_bytes_b64       # exact SQLite blob bytes, for blob/root_observation
blob_storage_class   # text | blob, for blob/root_observation
root_blob_id         # current root, when observed
```

The adapter captures every observed row in `meta` and `blobs`, not just blob
types the current decoder recognizes.  It captures each root observation with
the exact root blob bytes even if the root has already appeared as a generic
blob; the observation ties a provider-visible transcript ordering snapshot to
the raw blobs without inventing semantics for fields such as `3` or `8`.

The byte payload is a deterministic versioned wrapper around exact database
bytes. `observed_at` belongs in local source state, not the content-addressed
raw record: rereading unchanged evidence must produce the same bytes. The
wrapper is provenance; `*_bytes_b64` is the evidence. A future
decoder can therefore reconstruct raw rows and re-render from the archive
without rereading the user machine.  The adapter deduplicates only exact
`(kind, meta_key/blob_id, content hash)` records within an epoch.  It must not
deduplicate distinct unknown records because their meaning is not yet known.

### Rendering

The render lane is best effort and revisioned.

- It reads known current snapshot `field 1` message ordering and known JSON
  message/block forms.
- It emits canonical events only where the mapping is proven.
- Unknown snapshot fields, malformed/legacy blobs, nested branches, and
  unknown block types produce typed render gaps referring to their raw source
  record identities.
- It never fabricates per-event timestamps.  Cursor's durable store does not
  contain them.  Event order is provider order; observation time is only
  observation time.

Receipt-backed raw durability is the definition of archive success.  A failed
or pending render is visible diagnostics, not permission to retry by sending a
lossy legacy projection.

### Epochs and rewrites

Existing storage-v2 source epochs remain the durable receipt cursor.  Cursor
adapts them from file-oriented implementation details into logical-source
semantics:

- a WAL checkpoint does **not** rotate an epoch;
- a provider conversation identity mismatch creates a different source and
  may never inherit a managed binding;
- a changed store incarnation rotates the epoch;
- a non-prefix rewrite of the known root `field 1` ordered blob-id list
  rotates the epoch;
- a monotonic root extension stays in the same epoch;
- the durable cursor advances only from a valid storage-v2 receipt.

The implementation may retain the existing `source_epoch_registry` table for
this first cut, interpreting its incarnation and maximum-position fields for
logical sources.  It adds typed source descriptors and receipts rather than
continuing to add path-keyed special cases.

## Managed Helm binding

Helm owns a local PTY/control socket and remains Helm even if the archive
cannot bind.  A `LaunchBindingClaim` is an expiring local record containing:

```text
session_id, provider, launch time, expiry, cwd identity,
provider pid/start identity, and unambiguous provider-native binding evidence
```

The adapter may bind a logical source to that Helm `session_id` only after it
has verified provider-native evidence established by the interactive probe.
Time, a workspace path, process recency, and selecting the newest chat folder
are insufficient.  A missing or ambiguous match expires the claim and leaves
the source unmanaged; it must never attach a transcript to the wrong Helm
session.

The interactive probe proved this evidence: the launcher precreates a native
chat, reserves its ID, and the exact child hook reports both inherited
`LONGHOUSE_SESSION_ID` and Cursor `conversation_id`. The Machine Agent defers
the pending claim until the store observes that same provider ID, then binds
the source to the managed session. Ambiguous or missing evidence still fails
closed to an unmanaged source.

## Console ACP

ACP notifications are not `SessionIngest` payloads.  The engine appends their
raw JSON-RPC notifications to an engine-owned local source, immediately emits
provisional live/runtime events through the shared Rust outbox, and seals
receipt-backed storage-v2 records.  Durable receipt identity replaces matching
provisional preview identity.  Network failures retain the local source and
retry through normal v2 shipping; no provider-specific in-memory HTTP retry
policy survives.

## Migration

1. **Honesty cut.** Freeze false Cursor archive claims, reject Cursor Console
   on a storage-v2 Runtime Host, delete the legacy-containment probe, and add a
   repository guard against new production `/api/agents/ingest` writers.
2. **Probe.** Run the interactive Helm binding probe.  Do not implement a
   binding heuristic if it fails.
3. **Native read-only adapter.** Add fixtures covering current Cursor blobs,
   unknown blobs/blocks, WAL reads, root extension, non-prefix rewrite,
   checkpoint stability, and exact raw round-trip.  Ship Shadow only after
   receipts are proven.
4. **Helm binding.** Add `LaunchBindingClaim` only if the probe supplies
   deterministic evidence; otherwise leave Helm control-only.
5. **ACP.** Convert Console to the engine-owned source + runtime outbox, then
   re-enable its capabilities only with receipt-backed proof.
6. **Deletion cut.** Remove Python Cursor decoder/discovery/import/tailer,
   Cursor ACP legacy post/retries, path-keyed `session_binding`, legacy
   Cursor spool/replay/cutover code, and finally the legacy ingest endpoint
   once no supported provider produces it.

## Non-negotiable tests

- Exact captured `meta` and `blobs` bytes round-trip from sanitized fixtures.
- Unknown source material reaches a durable raw receipt and a typed render gap.
- Receipt failure cannot advance a record cursor or erase local evidence.
- WAL checkpoint leaves source epoch and receipt cursor stable.
- Root extension remains one epoch; non-prefix rewrite rotates it.
- A claim without probe-grade evidence cannot bind; an expired claim cannot
  bind later; a source identity mismatch cannot bind.
- Helm control survives adapter/transport failure without signaling the Cursor
  provider process.
- Cursor Console cannot start against a storage-v2 Runtime Host until its
  engine-owned source is enabled.
- A repository guard rejects newly introduced production writers to
  `/api/agents/ingest`.

## Explicit non-goals

- Fabricating timestamps that Cursor did not store.
- Guessing branch/subagent semantics before they are proven.
- A generic provider plugin framework or decoder subprocess system.
- Preserving pre-launch legacy ingest compatibility.
- Treating a dead wrapper, engine, archive transport, or control socket as
  authority to terminate a provider process.
