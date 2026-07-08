# Provider Live Proof Session Classification

## Problem

Provider live proof and no-reply canary sessions can leak into normal timeline,
menu bar, and search surfaces as if they were user work. After historical title
backfill, these rows get plausible but unhelpful titles such as "Token Echo
Verification Session." The title is not the real problem; the row is in the
wrong product lane.

These sessions are useful operational evidence. They should remain archived and
debuggable, but the default user experience should show human work, not
machine-generated proof traffic.

## Existing Concepts

Longhouse already has several related mechanisms:

- `environment=test/e2e`: canonical product filter. Timeline and
  `/api/agents/sessions` exclude test and e2e rows by default unless
  `include_test=true`.
- `internal_sessions.py`: shared SQL helper that hides synthetic canary/debug
  sessions by provider/project/device labels unless the caller explicitly asks
  for the canary provider.
- Provider live proof sidecars: OpenCode canary writes
  `provider-live-proof/sessions/opencode/<provider_session_id>.json` with
  `environment=test` and `reason=provider_live_canary`.
- `hide_autonomous`: hides task/subagent/no-user-message style work from normal
  listings. This is not the right primary axis for provider proof traffic.
- `sidechain`: subagent/child-thread lineage. This is not a synthetic/proof
  classification and should not be reused for this problem.

## Principle

Use the existing classification axes instead of inventing a new one:

1. Provider proof/canary rows are `environment=test`.
2. Internal proof labels are hidden by the existing internal-session filter.
3. User-facing surfaces keep their default `include_test=false`.
4. Debug/archive/operator surfaces can opt in with `include_test=true` or an
   explicit provider/project filter.

## Candidate Signals

A session should be treated as provider proof/internal test traffic when one or
more durable signals are present:

- `cwd` is under a provider-live canary workspace, such as
  `/.longhouse/canaries/provider-live/<provider>/.../workspace` or the repo
  build artifact path `/.build/canaries/provider-live/<provider>/.../workspace`.
- First non-empty user text starts with `LONGHOUSE_*_NOREPLY_`.
- A provider live proof classification sidecar exists for
  `(provider, provider_session_id)` and says `environment=test`.
- Existing canary labels already handled by `internal_canary_session_clause`.

Avoid broad content heuristics beyond the explicit `LONGHOUSE_*_NOREPLY_`
marker. A normal user can ask about tokens, proof, canaries, or no-reply; that
should not hide their session.

Treat sidecars as supporting evidence, not the primary online signal. The canary
may write the sidecar after the provider session is created, while `cwd` and the
no-reply marker are present in ingest payloads/events.

Some historical dogfood rows have reviewed proof worktree paths like
`longhouse-provider-live-proof-owner`. Those are narrow path markers, not broad
content heuristics, and can be treated as provider proof rows.

## Proposed Behavior

### Ingest

During `AgentsStore.ingest_session()`:

1. Normalize provider proof sessions to `environment=test` when durable proof
   signals are present.
2. Permit `environment=test` to replace a previous generic machine environment
   such as `cinder`, just as the current OpenCode canary path already does.
3. Keep the original provider, project, cwd, and events intact for auditability.
4. Run classification on both new-session creation and existing-session
   metadata refresh. Running it only on refresh leaves a window where fresh
   proof sessions can appear in default user surfaces.

This should be implemented as a small helper with a clear name near the existing
internal-session helpers:

```text
classify_provider_proof_environment(data, first_user_preview?) -> "test" | None
```

The helper should be deterministic and side-effect-free. Reading sidecars is
acceptable only from the local machine ingest path where the sidecar directory is
available; hosted cannot rely on local filesystem state after the fact.

### Listing

The existing default listing behavior should continue to do the main work:

- If `environment` is not explicitly supplied and `include_test=false`, exclude
  `environment in ("test", "e2e")`.
- Continue applying `internal_canary_session_clause()` to catch legacy canary
  labels and proof projects that were not normalized.
- Extend `internal_canary_session_clause()` with real durable labels only. The
  first cut should include cwd-based provider-live canary matching and reviewed
  historical proof worktree path markers. A project prefix such as
  `longhouse-provider-live-proof%` should be added only if the producer starts
  setting that project intentionally.

### Title Generation

Title generation should not be the primary filter, but it can avoid wasted calls:

- If a missing-title session is already classified as `environment=test/e2e` or
  matches `internal_canary_session_clause`, skip AI title generation by default.
- For debug/archive readability, deterministic titles are acceptable if needed:
  `OpenCode Provider Live Proof`, `Provider No-Reply Canary`, etc.

Do not make the title model responsible for deciding whether a session is
human-visible.

### Historical Repair

Add an explicit backfill/repair operation, not a startup migration:

1. Select sessions with provider proof signals that are not already
   `environment=test/e2e`.
2. Match on cwd and no-reply marker first. Include reviewed historical dogfood
   proof worktree paths after examples are inspected.
3. Dry-run by default and report counts plus example rows.
4. Apply in small batches through the write serializer or an operator script.
5. Upsert affected `timeline_cards` rows so hot listing behavior matches the
   session table.

For the current dogfood tenant, this backfill should target proof rows such as
provider-live canary cwd rows, no-reply marker sessions, and reviewed historical
proof-project rows.

## Tests

Focused tests should cover:

- Existing OpenCode provider live canary still reclassifies from machine
  environment to `test`.
- A freshly-created provider live canary session is classified as `test` on the
  first ingest, not only after a later refresh.
- `LONGHOUSE_*_NOREPLY_*` first user text classifies the session as `test`.
- Normal user text mentioning "no reply", "proof", or "canary" is not hidden.
- `include_test=true` can still retrieve proof sessions.
- Timeline-card listing and direct session listing agree.
- Title reconciler skips default AI titles for classified test/internal proof
  rows, or assigns deterministic titles only in an explicit repair path.

## Open Questions

- Should classification sidecars be ingested into a durable DB table, or is
  environment normalization enough for now?
- Should historical proof project prefixes be a one-time repair heuristic or a
  committed producer convention?
- Should the no-reply marker classify only OpenCode sessions, or all provider
  sessions with `LONGHOUSE_*_NOREPLY_*`?

## Recommended First Cut

1. Add a pure provider-proof classifier that recognizes provider-live cwd and
   `LONGHOUSE_*_NOREPLY_` markers.
2. Use that classifier during both new-session creation and existing-session
   metadata refresh to set `environment=test`.
3. Extend `internal_canary_session_clause()` with cwd-based provider-live canary
   matching for legacy rows that were not normalized.
4. Add a dry-run/apply repair command or one-off ops script for historical rows.
5. Skip AI title generation for classified internal/test proof rows.

This is small, uses existing nouns, and preserves proof data without letting it
pollute the normal product timeline.
