# ✅ COMPLETED / HISTORICAL REFERENCE ONLY

> **Note:** This feature has been implemented. Implementation details may have evolved since this document was written.
> For current documentation, see the root `docs/` directory.

---

Title: Usage Controls, Safety Caps, and Platform Hardening (PRD)

Objective

- Introduce immediate, low‑risk guardrails to control cost, abuse, and latency.
- Add lightweight observability to inform quotas and alerts.
- Tighten platform boundaries (CORS) for safer browser access.

Non‑Goals

- Full multi‑tenant rate‑limiting infra; Redis optional later.
- Complex billing/recon; only coarse cost estimates.
- External account provisioning or SSO changes.

Rollout Plan (phased)

1. Set OpenAI project budgets (external)
2. Output cap (LLM) + Model allowlist for non‑admins
3. Per‑user runs/day caps (non‑admins)
4. Token usage capture + threshold alerts (user and global)
5. Tighten CORS to exact origin(s)

Key Decisions

- Admin users are exempt from caps and model restrictions.
- Start with DB aggregation for usage/cost; add Redis counters later if needed.
- Default output cap 1000 tokens; configurable via env.

Environment Variables

- MAX_OUTPUT_TOKENS (int, default 1000; 0 disables)
- ALLOWED_MODELS_NON_ADMIN (csv; e.g., gpt-4o-mini)
- DAILY_RUNS_PER_USER (int; 0 disables)
- DAILY_COST_PER_USER_CENTS (int)
- DAILY_COST_GLOBAL_CENTS (int)
- ALLOWED_CORS_ORIGINS (csv of exact origins)

Tasks & Acceptance Criteria

Task A: Output Cap (LLM max tokens)

- Implementation
  - Add `MAX_OUTPUT_TOKENS` to settings.
  - Pass `max_tokens` into `ChatOpenAI` in `zerg/agents_def/zerg_react_agent._make_llm` when >0.
- Tests
  - Verify `ChatOpenAI` receives `max_tokens` equal to env value.
- Acceptance
  - All tests pass; cap applied for any model.
- Status: [x] Done
- Notes:
  - Added `max_output_tokens` to settings loaded from `MAX_OUTPUT_TOKENS` (default 1000).
  - Passed `max_tokens` to `ChatOpenAI` in `_make_llm`; defensively falls back if constructor doesn’t accept the param (test stubs).
  - Added tests `backend/tests/test_output_cap.py` verifying the constructor receives `max_tokens` from env.

Task B: Model Allowlist for Non‑Admins

- Implementation
  - Extend agents create/update routes to enforce allowlist when `current_user.role != ADMIN`.
  - If disallowed/empty: reject (422).
  - Optional: Filter `/api/models` list for non‑admin to allowed subset.
- Tests
  - Non‑admin cannot create/update agent with disallowed model.
  - Admin unaffected.
- Acceptance
  - Correct HTTP status and behavior; tests pass.
- Status: [x] Done
- Notes:
  - Added `ALLOWED_MODELS_NON_ADMIN` to settings.
  - Enforced allowlist in agent create/update routes for non‑admin users (admins unrestricted).
  - Filtered `/api/models` response for non‑admins to allowed subset when configured.
  - Added tests: non‑admin blocked on disallowed model; allowed model passes; admin bypass; models endpoint filtering.

Task C: Per‑User Runs/Day (non‑admins)

- Implementation
  - Add guard helper that computes today’s run count per user via `AgentRun` joined to `Agent.owner_id` and UTC date.
  - Enforce in both:
    - `services/task_runner.execute_agent_task` (task runs)
    - `routers/threads.run_thread` (chat runs)
  - Admins exempt.
  - Env: `DAILY_RUNS_PER_USER` (0 disables).
- Tests
  - Non‑admin blocked after N runs; admin allowed.
- Acceptance
  - Correct denial at threshold; HTTP 429/403 chosen consistently.
- Status: [x] Done
- Notes:
  - Added shared helper `zerg.services.quota.assert_can_start_run` to enforce `DAILY_RUNS_PER_USER` (0 disables).
  - Wired into `services/task_runner.execute_agent_task` and `routers/threads.run_thread`.
  - Added tests (`test_daily_runs_cap.py`) covering non‑admin blocking, admin exemption, and both thread/task entrypoints.

Task D: Token Usage Capture + Cost Estimate

- Implementation
  - In `AgentRunner.run_thread`, aggregate `prompt_tokens`/`completion_tokens` from `AIMessage.response_metadata.usage` across model turns.
  - Add `MODEL_PRICES` map (per 1K tokens in/out) and compute `total_cost_usd`.
  - Persist via `crud.mark_finished(... total_tokens, total_cost_usd)`.
  - Fallback to estimating via `tiktoken` when provider usage missing.
- Tests
  - Inject fake usage metadata; validate persisted totals.
- Acceptance
  - Totals appear on `AgentRun`; tests pass.
- Status: [x] Done (usage); [ ] Pricing map population
- Notes:
  - Aggregates provider-supplied usage from AIMessage.response_metadata/additional_kwargs only (no estimates).
  - Persists `total_tokens` on AgentRun when available; otherwise leaves NULL and logs.
  - Adds pricing catalog scaffold `zerg/pricing.py` (empty except gpt-mock).
  - Pricing is loaded from external JSON when `PRICING_CATALOG_PATH` (or `PRICING_CATALOG_JSON`) is set. Shape:
    - `{ "<model_id>": [<in_price_per_1k>, <out_price_per_1k>] }` or
    - `{ "<model_id>": {"in": 0.001, "out": 0.002} }`.
  - Cost is computed only when a model has an explicit entry; otherwise left NULL.
  - Tests cover metadata present and missing; cache cleared per test for deterministic behavior.

Task E: Budget Thresholds (User + Global, per day)

- Implementation
  - In the same pre‑check as runs/day, aggregate today’s user and global cost from `AgentRun.finished_at`.
  - Log warning at ≥80%; deny non‑admin at ≥100%.
  - Admins exempt.
  - Env: `DAILY_COST_PER_USER_CENTS`, `DAILY_COST_GLOBAL_CENTS`.
  - Aggregation uses `AgentRun.total_cost_usd` (unknown costs are ignored).
- Tests
  - Threshold triggers and denial behavior validated.
- Acceptance
  - Logs at 80%; denial at 100%; tests pass.
- Status: [ ] Not started

Task F: CORS Tightening

- Implementation
  - Use exact origins from `ALLOWED_CORS_ORIGINS` in prod; wildcard only in dev/test.
  - Update error handler to only set `Access-Control-Allow-Origin` when request origin is allowed; add `Vary: Origin`.
  - Default `allow_credentials=False` (cookie flows can opt-in later).
- Tests
  - Minimal integration checks for CORS headers with allowed vs. disallowed origins.
- Acceptance
  - Headers correct; no wildcard in prod unless explicitly configured.
- Status: [ ] Not started

Task G: Admin Exemptions Audit

- Implementation
  - Ensure all caps and allowlists skip `role=ADMIN`.
  - Unit tests assert exemption across tasks B–E.
- Status: [ ] Not started

Task H: Cleanup & Registry

- Implementation
  - Remove unused `OpenAI` client in `routers/agents.py`.
  - Ensure model registry includes any IDs referenced by allowlist defaults.
- Status: [ ] Not started

Risks & Mitigations

- Race conditions on counters: start with DB aggregation which is eventually consistent per request; add Redis `INCR` later for atomicity.
- Provider param naming drift: monitor `langchain-openai` releases; use `max_tokens` compatible with current version.
- Over‑blocking: start with warning logs before turning denials on by default; ship with conservative thresholds.

Operations

- Set OpenAI budgets in the dashboard per project.
- Configure env vars in production and staging.
