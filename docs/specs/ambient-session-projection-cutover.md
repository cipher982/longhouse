# Ambient Session Projection Cutover

Status: Proposed for steelman review
Owner: Machine Agent / Runtime Host / macOS ambient product
Last updated: 2026-07-22
Related: `local-truth-projection.md`, `macos-menu-bar-state-model.md`,
`managed-provider-session-contract.md`

## Decision

Replace the menu bar's compound `local-health --fast` snapshot with two
explicit, independently-owned projections:

1. **Machine Agent local projection** — one atomically written
   `engine-status.json` containing the complete local truth needed to render a
   Helm/Console/Shadow row and local system facts. It is read locally, has no
   network dependency, and is the normal native menu-bar input.
2. **Runtime Host session stream** — one authenticated SSE connection whose
   initial replay and subsequent deltas carry canonical session facts,
   including AI titles. It is an overlay, never a prerequisite for rendering
   local state.

The menu must never select a different set of user-visible facts because a
caller chose a performance flag. “Fast” is deleted from the ambient product
contract; bounded diagnostics remain explicit commands.

## Product outcome

On a Mac with managed sessions, opening Longhouse immediately shows each local
session with an honest fallback title and current local control/liveness state.
Within the normal Runtime Host stream connection, the fallback title is
replaced by the AI title generated from the first durable user message. A
network outage delays enrichment only; it cannot turn a known local session
into `ACTIVITY UNKNOWN`, hide it, or block the menu.

The title service has one job: produce a short, stable 2–4 word title from the
first actual user prompt. The Runtime Host owns its state machine and result.
Provider launchers may retain a local prompt fallback, but no client makes a
per-session title RPC to render a menu.

## What we found

### One flag currently changes several unrelated contracts

`collect_local_health(..., fast=True)` decides process validation, provider
proofs, provider release checks, hook/binding diagnostics, Cursor discovery,
and—until the tactical repair—Runtime Host title enrichment. The first group
is diagnostic cost; the last is product data. A Boolean cannot express that
boundary and made the desktop's default invocation silently omit AI titles.

### Presentation does redundant work and waits on the network

The desktop's normal path shells out to `longhouse local-health --fast --json`
at launch and every refresh. That Python path now makes one Runtime Host
`GET /api/agents/sessions/{id}` per managed session to obtain titles. The
desktop then makes the same N-request bootstrap in `SessionProjectionStream`
before opening `/api/agents/sessions/stream?skip_initial_replay=false`, whose
initial replay already supplies those same session deltas. This creates two
independent N fan-outs and violates the menu contract that refresh not depend
on the network.

### The local status file contains facts that consumers must reassemble

`engine-status.json` emits a resolved session list and a separate fresh
`phase_ledger`. The Python reader had dropped the ledger while projecting
managed sessions, producing the observed `ACTIVITY UNKNOWN` rows despite fresh
`thinking`/`running` evidence. The tactical repair merges the rows in Python,
but a consumer should not need to reconstruct a session projection from two
parallel collections.

### Title fallback is being mistaken for a title-generation failure

The AI title service is already producing canonical `ready/ai` titles through
the Runtime Host. Some visible prompt titles were local fallback values. In at
least one inspected Codex transcript, the apparent “later” text was actually
the first durable user message after provider-injected context. The system must
make this provenance visible (`prompt` vs `ai`) rather than infer ordering from
the rendered string.

## First-principles model

| Fact | Owner | Transport | Allowed fallback |
| --- | --- | --- | --- |
| local provider/process/bridge evidence, local workspace, Helm control path | Machine Agent | atomic local status file + file monitor | last coherent local value, marked with age |
| local phase observation | Machine Agent | embedded in that session row | `unknown` only when no fresh local observation exists |
| managed mode and canonical presentation/control facts | Runtime Host catalog | one session SSE stream | retain last stream value with age/provenance |
| short AI title and title state/source | Runtime Host title pipeline | same session SSE stream | local prompt/workspace fallback, explicitly `prompt` |
| deep verifier output, provider versions, transcript/hook scans | explicit diagnostics | `longhouse doctor` / diagnostic commands | no ambient UI dependency |

No layer is allowed to derive or overwrite another layer's fields. The Swift
reducer merges by field ownership and freshness; it does not replace a complete
snapshot because an unrelated source refreshed.

## Target contracts

### `engine-status.json`: complete local projection

Add a versioned `local_projection.sessions[]` collection. Each row is already
joined by the Machine Agent and contains: session id, provider, mode, workspace
label/path, launch/control-path evidence, local liveness, local phase and
`phase_observed_at`, local activity time, and per-field observation timestamps.
`phase_ledger` may remain temporarily for diagnostics, but is not a required
input to normal UI projection.

The status writer is a serializer of retained coherent state. It must not run
provider discovery, transcript scans, `ps`/`lsof`, provider version commands,
OpenCode title discovery, Runtime Host HTTP, or title generation. Startup/wake
and bounded reconciliation populate retained observations; publication simply
writes the latest coherent projection atomically.

### Runtime Host: one stream, including bootstrap

`/api/agents/sessions/stream` initial replay is the sole remote bootstrap. It
must include the same title/title-state/source and canonical session facts as a
detail read for every locally relevant session. The desktop opens it once,
filters to local ids, then consumes deltas. Delete the per-session Swift
`bootstrap()` requests and delete Python per-session title enrichment from
ambient local health.

The stream is best-effort enrichment. Reconnects use bounded backoff and a
stale stream does not erase local rows or local fallback titles.

### Native desktop input

At boot, Swift decodes the local status file directly, then `LocalStatusMonitor`
applies incremental local projection updates. It starts the Runtime Host stream
once local session ids and connection metadata are present. The Python CLI is
kept only as a compatibility/diagnostic surface; it is not the normal menu-bar
data plane and it does not poll every 30 seconds.

The desktop reducer retains local and remote fragments by session id. It must
not preserve an old complete CLI snapshot over a fresher local projection or
vice versa.

### Titles

On receipt of the first durable `user` content event for a session, the Runtime
Host enqueues exactly one idempotent short-title request. The request uses the
configured low-latency title model, a strict short-title schema, and a bounded
timeout. It records `pending`, `ready/ai`, or a retryable failure with attempt
and age. Replays/deduplication are keyed by `(session_id, first_user_event_id)`;
later user messages cannot replace a ready AI title unless the user explicitly
renames the session.

Title title generation must not sit on the ingest acknowledgement path. Its
eventual result emits the normal session delta, so every client converges
without polling. The prompt fallback remains immediately available and is
always labelled by `title_source`.

## Non-options

- Do not increase a per-session HTTP fan-out or its concurrency to make the
  existing fast path appear reliable.
- Do not put Runtime Host title data in the Machine Agent's durable local
  projection; it creates replicated canonical state and stale ownership.
- Do not make a network stream mandatory for the first truthful local render.
- Do not retain `fast` as a hidden profile with different row fields.
- Do not invent a new local database, generic event bus, or desktop process
  scanner. Existing engine retained state, atomic file, monitor, and SSE seams
  are sufficient.

## Migration plan

1. **Projection schema and writer.** Add `local_projection.sessions` in Rust,
   populated from retained resolved observations and fresh phase evidence at
   projection build time. Keep legacy `sessions`/`phase_ledger` for one
   compatibility release. Add engine tests proving phase is embedded and a
   writer never invokes discovery.
2. **Reader compatibility.** Teach Python and Swift to prefer the new embedded
   rows and fall back to legacy rows only when the new schema is absent. Remove
   Python's Runtime Host title enrichment. Convert `local-health --fast` into a
   deprecated compatibility spelling of the same complete local read; keep
   expensive diagnostics only behind explicit doctor/deep commands.
3. **Desktop cutover.** Make native status-file decode the normal source,
   remove periodic CLI refresh and cache it only as last-good recovery. Start
   one SSE stream after local ids are known. Remove `SessionProjectionStream`
   per-id bootstrap entirely.
4. **Title-pipeline hardening.** Assert first-user-event idempotency, timeout
   behavior, provenance, and delta emission. Instrument queue-to-title latency
   and failure reason without exposing a degraded placeholder as a title.
5. **Remove compatibility.** After one released client generation consumes the
   new local projection, remove legacy rejoin code and the ambient fast mode.

## Acceptance gates

- Menu cold render reads only the local file and produces local rows in under
  50 ms; a disconnected Runtime Host does not change that result.
- One Runtime Host stream connection, zero per-session HTTP requests, and zero
  Runtime Host requests from local-health during ordinary menu operation.
- A fresh local phase becomes a row state in the status file; no client joins
  `phase_ledger` to recover it.
- Every managed row renders a title immediately (fallback allowed), then
  converges to `ready/ai` through a stream delta when available.
- Duplicate/replayed ingest events make at most one title request for the
  first-user event; a later prompt cannot overwrite a ready AI title.
- Deep diagnostics can be slow without delaying the status writer or ambient
  UI. Network loss and provider discovery failure retain explicitly-aged last
  coherent local facts.

## Regression suite

- Rust status-projection fixtures: managed sessions with fresh thinking,
  running, absent, and stale phase evidence; assert a single embedded row
  shape and atomic write.
- Python compatibility tests: new projection is read without process scans or
  Runtime Host HTTP; legacy file still reads correctly during migration.
- Swift reducer tests: local-first render, remote title overlay, remote outage,
  stream reconnect, and field-ownership/freshness conflicts.
- Swift URLProtocol integration: exactly one stream request, no
  `/api/agents/sessions/{id}` bootstrap calls, and initial replay updates AI
  titles.
- Server tests: title request fires once for the first durable user event,
  handles timeout/retry, carries `title_state`/`title_source` in initial replay
  and delta, and never replaces a ready title from a later prompt.
- End-to-end fixture: managed Helm session shows `thinking` locally before
  Runtime Host connectivity, then receives its AI title with no panel refresh
  or process scan.

## Tactical changes already landed

`b552af76d` merges fresh phase-ledger evidence in the legacy Python reader,
which fixes the observed false unknown activity. `91d07ab46` makes the current
menu fast snapshot fetch AI titles for all rows, fixing immediate visibility.
They are containment fixes, not the target architecture: phase merging moves
to the writer and the title HTTP fan-out is removed in migration steps 1–3.
