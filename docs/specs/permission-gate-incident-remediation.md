# Permission-Gate Incident Remediation

Status: implementation-ready plan
Owner: managed-provider / session-state surfaces
Date: 2026-07-22
## Decision

Fix the proven Cursor permission-gate residue first. Do not make a broad
provider-adapter or permission-authority redesign a prerequisite.

The active [Cursor permission policy](cursor-permission-policy.md) remains the
product contract: Helm defaults to `auto_approve`; `provider_local` and
`remote_human` are explicit choices. The current launch boundary already clears
the gate environment for autonomous policies and keeps an unrecorded legacy
claim dormant. This work verifies those protections and closes the remaining
write, lifecycle, repair, and diagnostic defects.

## Incident Evidence

The managed Cursor Helm session `28df78b6-8da9-4c36-98c2-4b4ad0ad343b` is a
stock Cursor conversation. Its local phase is `idle` and its final Cursor turn
completed at 2026-07-22T20:26:41Z. Its Shell results use Cursor's unrestricted
local policy.

The served catalog has a stale row opened 2026-07-20T16:14:01Z:

```text
interaction id:   7c72257b-f5d9-5eaf-85c7-e0cf8e59251f
provider:         cursor
runtime_key:      cursor:28df78b6-...
kind/status:      permission_prompt / pending
source:           claude_permission_gate
reply transport:  claude_pretooluse_pull
title/summary:    Permission: Shell / Claude wants to use Shell.
expires_at:       null
```

Cursor did not ask Claude for permission. A Cursor hook reached a formerly
Claude-shaped shared endpoint: it preserved the submitted Cursor provider but
persisted Claude provenance, transport, and copy. Catalog reads treat a null
deadline as indefinitely pending, so the stale row still overrides the real
idle state in the web and desktop attention surfaces.

The historical writer was corrected in part after this row was created:
current source chooses `cursor_permission_gate` and Cursor copy. It still
unconditionally writes `REPLY_TRANSPORT_CLAUDE_PULL` for Cursor, and legacy
null-expiry rows remain forever-pending.

## Goals

1. New Cursor held-permission records have Cursor source/copy/poll transport,
   a required deadline, and validated provider/transport pairing.
2. A terminal held request atomically clears its matching runtime pending
   pointer and propagates the change to API/SSE/APNS/iOS/web/desktop consumers.
3. The incident row is repaired through catalogd with an auditable, exact-ID
   operation. No historical interaction is deleted.
4. Autonomous Cursor Helm is re-proven to make zero held-permission requests;
   no unproven launch-boundary redesign is introduced.
5. Hosted session debug reads the catalog database that serves live state.

## Non-goals

- Changing default Cursor policy or silently changing a resumed session's
  permission authority.
- Broadly renaming the Claude route/service, a plugin architecture, or a
  multi-provider adapter framework.
- Sweeping all legacy null-expiry requests. Valid old Claude records require a
  separate incident/evidence review.
- Generic "idle means resolve every question" behavior.

## Required Design

### Closed provider contract

Keep the existing shared interaction model, but define a closed validation
table for new writes:

| Provider | registration adapter | source | delivery transport | deadline |
| --- | --- | --- | --- | --- |
| Cursor | `cursor_hook` | `cursor_permission_gate` | `cursor_permission_poll` | required |
| Claude | `claude_pretooluse` | `claude_permission_gate` | `claude_pretooluse_pull` | required |

The endpoint rejects mismatched provider/source/transport values before
catalogd writes. Provider display copy is derived from the validated provider
at projection time, not trusted from a persisted summary. The old route path
may remain as a compatibility alias; names alone are not behavior.

Cursor's current unconditional Claude transport constant is replaced with the
validated Cursor poll value. Claude and OpenCode semantics remain unchanged
except for shared deadline and terminalization guarantees.

### Launch-boundary proof, not a parallel policy model

Do not add a second `PermissionAuthority` vocabulary. Reuse the canonical
Cursor `permission_policy` and its current child-environment construction.

For `auto_approve` and `provider_local`, tests must prove that the launch
removes **all** Longhouse gate variables, including
`LONGHOUSE_PERMISSION_HOOK_ENABLED`, `LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S`,
`LONGHOUSE_HOOK_URL`, and `LONGHOUSE_HOOK_TOKEN`, while preserving unrelated
provider auth/proxy/shell environment. The globally installed fail-closed hook
must take its no-I/O dormant path under those policies.

For `remote_human`, keep the existing session-scoped hook token path, but
strengthen its server validation before relying on it for new behavior: the
capability/token must bind session, provider, launch, Cursor conversation,
policy, expiry, and a unique capability ID. Registration cannot accept those
identity fields as unchecked caller claims. Reject stale, forged, wrong-launch,
wrong-conversation, revoked, or expired capabilities. This is a narrow
hardening of the existing remote-human capability, not a new policy schema.

### Exact invocation lifecycle

A held request must persist immutable lineage fields rather than hiding them in
an opaque request hash:

```text
session_id, provider, launch_id, provider_conversation_id,
generation_id, invocation_id, capability_id, expires_at
```

Retries of the same lineage return the original row and its terminal result;
they never re-pend it. A re-ask uses a new invocation ID. Decide and test one
of two supported Cursor policies explicitly: serialize one held Shell/MCP call
per runtime, or support concurrent holds keyed by invocation. Do not retain
`single_active` as undocumented accidental serialization.

Only exact provider completion evidence for the same Cursor launch,
conversation, generation, and invocation may supersede a held Cursor
permission request. Generic idle state and transcript progress cannot resolve a
structured question or a live remote-human hold.

Every permission registration has `expires_at`; null expiry is rejected for new
writes. Catalogd owns a deadline sweeper/mutation that marks elapsed pending
requests terminal, clears the matching runtime pointer, advances revision, and
emits normal invalidation/notification paths. Read-time filtering is defensive
only; it must never be the canonical cleanup mechanism.

### Repair and ordering

Close the writer before repairing data:

1. Deploy the server-side closed provider validation, Cursor transport fix, and
   required deadline support.
2. Deploy capability/lineage-compatible hooks and launcher; publish a rolling
   compatibility matrix for old/new server and hook combinations. Old hooks
   must fail safely and cannot recreate malformed records after the server
   rejects them.
3. Run an exact incident repair through a narrow catalogd operator RPC—never
   direct SQLite writes while catalogd is live.

The first operator command accepts only interaction
`7c72257b-f5d9-5eaf-85c7-e0cf8e59251f`, requires its expected immutable
digest/timestamps and `status=pending` as compare-and-set preconditions, and
requires newer corroborating Cursor completion evidence for the same session.
It sets `expired` with reason `legacy_provider_gate_contract_invalid`, clears
the exact runtime pointer in the same catalogd transaction, records operator
identity/UTC time/before-after digest, and emits an audit receipt.

It starts `--dry-run`; apply requires an explicit reviewed receipt. Take a
backup first. A broader legacy sweep is deferred to a separately approved
predicate and never targets every null-expiry request.

### Served-state and diagnostic behavior

`resolve_interaction`/expiry owns clearing a matching
`live_runtime_state.pending_interaction_*` pointer; extend that existing
catalogd pattern rather than creating another state authority. The web composer,
menu bar, API projections, iOS, APNS/Live Activity, and SSE subscriptions use
that canonical state and invalidate immediately on terminalization.

Update `scripts/ops/hosted-session-debug.sh` to discover/query the active
catalog database (currently `longhouse-live.db`) and report its exact path,
container/image identity, `live_sessions`, `live_runtime_state`,
`live_interaction_requests`, and `live_timeline_cards` records. A zero-byte
legacy `longhouse.db` is a hard diagnostic mismatch, not an empty session.

## Implementation Sequence

1. Add a hermetic fixture for the malformed Cursor/Claude row plus a regression
   test for the served false-attention projection.
2. Fix the current writer: validated Cursor source/transport/copy, required
   expiry, and compatibility reads for historical rows.
3. Add lineage, retry/concurrency semantics, catalogd deadline terminalization,
   atomic pointer clear, and consumer invalidation tests.
4. Re-prove the existing Cursor launch boundary; harden remote-human capability
   validation only where the proof finds a remaining forgery path.
5. Add the catalogd operator RPC, execute its dry run, review the receipt, then
   apply the exact incident repair after the writer is deployed.
6. Update hosted debug and run autonomous plus explicit remote-human canaries.

Each change is an atomic commit. The exact repair is a deployment operation,
not a startup migration or a direct database edit.

## Acceptance Criteria

- New Cursor writes cannot carry Claude source/transport/copy; catalogd rejects
  invalid combinations.
- New held permission rows always have immutable lineage and a deadline.
- Duplicate registration after terminalization returns the terminal outcome;
  it never recreates pending state.
- Deadline expiry and exact completion clear the runtime pointer transactionally
  and notify all served consumers.
- Bare Cursor Helm and explicit `auto_approve`/`provider_local` make zero
  permission registrations despite inherited gate variables; the dormant global
  Shell and MCP hooks do no I/O.
- `remote_human` rejects forged/stale/wrong-launch/wrong-conversation/revoked
  capability use and proves Allow, Deny, timeout, and hook crash behavior.
- The exact-ID repair is dry-run first, compare-and-set guarded, backed up,
  auditable, idempotent, and does not touch other historical records.
- Hosted debug reports the live catalog evidence for this incident rather than
  the empty compatibility database.

## Review Disposition

Cursor/Grok approved the narrow repair conditionally and identified that current
launch hardening is largely already present; the superseded broad authority and
adapter redesign has been removed. Sol required capability binding, explicit
lineage, catalogd-owned expiry, exact-ID repair, compare-and-set/audit,
writer-before-repair rollout, and consumer coverage; each is incorporated
above. Both reviewers agreed that generic idle/transcript progress must not
silently resolve a valid live permission request.
