# Dense recall: episode embeddings on the live-catalog path

**Date:** 2026-07-26
**Follow-up to:** `cross-session-recall-postmortem.md` §"Out of scope" — that
postmortem fixed the lexical tool-surface gap and explicitly deferred semantic/embedding
recall quality. This spec covers that deferred work.
**Status:** implementation shipped (`169bcb329`), reviewed by `hatch codex sol`
(disposition below), backfill/live-e2e/benchmark still open.

Delete this file once the phase ships and the eval results are folded into
`eval/recall/README.md` — it is a working note, not permanent documentation.

---

## 1. Why

`eval/recall/` (89 hand-labeled queries, gold sessions mechanically derived from
distinctive terms) measured the existing lexical-only recall path on hosted `david010`:

```
false 'nothing found'    73.7%   <- gate
correct abstention        7.7%
  exact          18/27 (67%)
  paraphrase      1/24 (4%)
  causal          0/15
  supersession    1/10
  absent          1/13
```

Lexical search cannot close the paraphrase/causal/supersession gap by construction — it
can only match tokens that are literally present. That is the retrieval-quality gap the
postmortem named and set aside.

## 2. What was already there

`SessionEmbedding` / `embed_session()` / `EmbeddingCache` / `session_hybrid_search.py`
existed pre-phase as a dormant pipeline: turn-level chunking, OpenAI-compatible embedding
calls, in-memory cosine similarity. It was reachable only from a legacy non-live-catalog
code path and from the session-listing hybrid endpoint — never from `/api/agents/recall`
in live-catalog mode, which is what hosted tenants (and this eval) actually run. Config
pointed at `openai/text-embedding-3-small`.

## 3. What changed (commit `169bcb329`)

1. **Chunking fix** — `iter_turn_chunks` previously closed a chunk at the first assistant
   reply even if the agent kept working (more tool calls, more assistant turns) before the
   next user message. Rewritten to span the full episode: one user event through
   everything up to (not including) the next user event. Matches the round-level boundary
   LongMemEval (ICLR 2025) found beats fixed windows and whole-session chunking.
2. **Live-catalog wiring** — `recall_sessions`'s `live_catalog_enabled()` branch (the one
   hosted tenants hit) previously called storage-v2 lexical search and returned before the
   embedding code below it could ever run. Added `_semantic_recall_matches` (dense lane,
   scoped with the same test/automation/canary/provider-proof filters as the lexical
   branch above it) and `_rrf_merge_recall_matches` (reciprocal rank fusion by session_id).
   Best-effort throughout — no config, no vectors, or a failed API call all degrade to
   lexical-only silently. Skipped under `TESTING=1` since embedding generation always
   makes a live network call no existing test mocks.
3. **Model swap** — `config/models.json` embedding default: `openai/text-embedding-3-small`
   → `qwen/qwen3-embedding-8b` via OpenRouter, still 256 dims (Matryoshka-capable, MTEB
   Code 80.68, ~13x cheaper for the ~140K-chunk corpus). Verified live against the
   OpenRouter endpoint before committing.

Tests: 3658 passed (full backend lite suite), including a new regression test for the
episode-boundary fix.

## 4. Sol review disposition

Ten findings, ranked by Sol as 1 critical / 4 high / 5 medium. Fixed (commit after
`169bcb329`):

- **Critical — live-catalog crash risk.** `_semantic_recall_matches` called
  `database_module.get_session_factory()` outside its own try/except; that factory is not
  guaranteed to initialize in live-catalog mode, so `mode=auto` risked a `ValueError`
  instead of degrading to lexical-only. Fixed: the whole embedding+DB+search body now runs
  inside one `asyncio.wait_for(...)` wrapped in a single try/except.
- **High — no request-deadline awareness.** The 5s recall route timeout and the
  embedding call's independent 10s timeout weren't coordinated; a slow embedding call could
  turn a successful lexical response into a 503. Fixed: `_semantic_recall_matches` now
  takes `timeout_seconds` from the route's own `remaining_budget()` and bounds itself with
  `asyncio.wait_for`.
- **High — zero test coverage of the new path.** `TESTING=1` short-circuited before the
  embedding call, DB factory access, filtering, or RRF ever ran, so the crash above shipped
  invisibly. Fixed: `tests_lite/test_recall_semantic_lane.py` forces the path past that
  guard with mocked embeddings and directly reproduces the crash scenario, the timeout
  path, and the snippet-indexing bug below.
- **High — snippet used the wrong index space.** `event_start`/`event_end` index the
  *clean* (content-bearing) projection; `_fetch_episode_snippet` queried raw durable rows
  with the same offsets, landing on the wrong episode for any tool-heavy session. Fixed:
  it now builds the same clean projection the embedding pipeline built and indexes into
  that.
- **Medium — head truncation cuts off the end of long episodes**, exactly where a
  diagnosis/fix conclusion usually lives. Fixed: switched to the existing `sandwich`
  truncation strategy (head + tail, marker in between) rather than inventing a new one.
- **Medium — not real RRF.** Semantic results were pre-filtered to exclude anything
  lexical already returned, so no session could ever get agreement credit from both lanes,
  and ties always favored lexical insertion order. Fixed: semantic now returns its full
  ranked list; `_rrf_merge_recall_matches` sums scores across both lanes and keeps
  whichever match object has the *better individual rank* for evidence display.
- **High — vector-length not validated.** A provider silently ignoring `dimensions`
  would store a native-size vector mislabeled with the configured dims, later silently
  skipped by the cache loader. Fixed: `generate_embeddings` now raises immediately on a
  shape mismatch, at the one point that knows both the expected and actual size.

Deliberately not changed, with reasoning:

- **High — no owner/tenant scoping on `SessionEmbedding`/`AgentSession` queries.** True,
  and would be a real tenant-isolation leak on a shared host. Today `require_single_tenant()`
  gates the whole `recall_sessions` route, matching the rest of this codebase's
  single-tenant-per-instance model — the semantic lane has the same scoping property as
  everything else in the function, not a weaker one. Flagging here so it isn't forgotten if
  shared-host multi-tenancy is ever revisited.
- **Medium — episode-boundary edge cases** (consecutive same-role events merge before
  boundary detection runs; a leading assistant-only turn now becomes a chunk where it was
  previously silently dropped). The merging behavior is inherited from `_iter_clean_turns`,
  unchanged by this commit. Capturing previously-dropped leading assistant content is a
  strict improvement, not a regression. Not fixing either as part of this pass.
- **Medium — visibility-filter parity between lanes.** The semantic lane's filter set
  (canary/provider-proof/hidden-flag) is a superset of the lexical branch's
  (test/e2e/automation-environment), meaning it hides *at least* as much, never less. Sol's
  own note confirms the correct primitive for Hatch automation is the hidden flag (which
  semantic checks), not the environment string (which is what lexical checks) — so this is
  an existing lexical-side gap surfaced by the comparison, not something the new code
  introduces or should paper over here.
- **Medium — forced-corpus-migration risk from the model swap.** Verified empirically:
  `session_embeddings` does not exist yet on hosted `david010` (checked via sqlite before
  backfill). This is a cold start, not a migration; the concern doesn't apply.

Verification after fixes: 3665 passed (full backend lite suite, up from 3658 — 7 new
tests), ruff clean.

## 5. Remaining work (the actual gate)

Nothing above has touched real data yet — it's code, not corpus.

1. Trigger `POST /backfill-embeddings` against hosted `david010` with the new config.
2. Verify completeness: row counts, `needs_embedding` cleared, spot-check decoded vectors.
3. Live e2e: real `/api/agents/recall?mode=auto` calls against david010, confirm semantic
   hits actually surface (not just lexical passthrough) and latency stays sane.
4. Re-run `eval/recall/run_eval.py` against hybrid mode. Compare against the lexical
   baseline above. This is the release gate — the point of all of this was to move that
   73.7% number, not to ship code that compiles.
5. Report before/after, scope anything deliberately deferred (no sub-chunking for
   oversized episodes, no incremental-reembed verification) as explicit follow-ups.
