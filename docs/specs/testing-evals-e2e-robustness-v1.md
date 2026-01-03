# Testing Robustness Spec (Evals + E2E Journeys + LLM Judge)

**Status:** Proposed
**Date:** 2026-01-02
**Owner:** David

## Summary

This spec defines a **practical** way to make Swarmlet/Zerg tests more behavior-robust without turning the suite into a flaky, expensive “LLM oracle”.

Key moves:
- Treat **deterministic assertions** as the default (events, tools, state, UI markers).
- Use **LLM-as-judge** only where semantics matter (final user-visible response quality).
- Keep a small “live journey” slice for end-to-end UX validation; keep the rest hermetic/deterministic.

## Current Reality (Repo-Verified)

### Backend tests
- Backend tests use **Postgres via testcontainers** (not in-memory SQLite), with session-scoped schema creation + TRUNCATE per test: `apps/zerg/backend/tests/conftest.py`.
- “Live” backend tests exist and already validate tool-call efficiency by reading SSE: `apps/zerg/backend/tests/live/test_prompt_quality.py`.

### Evals (pytest + YAML)
- Evals are implemented and already support `llm_graded` (LLM-as-judge) in live mode: `apps/zerg/backend/evals/`.
- Evals support deterministic assertions against:
  - tools called (`tool_called`)
  - workers spawned (`worker_spawned`)
  - worker artifacts and tool events (`artifact_*`, `worker_tool_called`)
  via `apps/zerg/backend/evals/asserters.py` and `apps/zerg/backend/evals/runner.py`.
- **Doc drift:** `docs/specs/eval-dataset.md` describes earlier counts/behavior that no longer match `apps/zerg/backend/evals/datasets/*.yml`.

### Playwright E2E
- Playwright E2E runs with **per-worker Postgres schema isolation** via `X-Test-Worker` header and pre-created schemas:
  - `apps/zerg/e2e/test-setup.js`
  - `apps/zerg/e2e/spawn-test-backend.js`
  - `apps/zerg/e2e/tests/fixtures.ts`
- **Doc drift:** `apps/zerg/e2e/TEST_SUITE_OVERVIEW.md` still claims SQLite-per-worker isolation.
- E2E already contains both:
  - **deterministic** “scripted model” tests (strong assertions): `apps/zerg/e2e/tests/evidence-mounting-deterministic.spec.ts`
  - **weak semantic** checks (“assistant has content”): e.g. `apps/zerg/e2e/tests/evidence-mounting.spec.ts`, `apps/zerg/e2e/tests/chat_perfect.spec.ts`
- E2E already has OpenAI usage patterns (currently focused on visual analysis): `apps/zerg/e2e/utils/ai-visual-analyzer.ts`, `apps/zerg/e2e/tests/visual-ui-comparison.spec.ts`.

## Goals

1. **Tier 3 (Agent behavior, no UI):** Expand “live evals” to catch supervisor/worker behavior regressions with *mostly deterministic assertions*, plus limited LLM grading.
2. **Tier 4 (User journeys, real UI):** Add **a small number** of Playwright “live journeys” that run the full stack and use LLM-as-judge for the final user-visible semantics.
3. Make failures **actionable**: when a semantic judge fails, preserve enough artifacts (run id, transcript, key SSE/tool events, screenshot) to debug quickly.
4. Keep the default CI experience **stable**: most tests remain hermetic/deterministic; live tests run only when explicitly enabled and secrets exist.

## Non-goals

- Replacing deterministic assertions with LLM grading everywhere.
- Making the entire Playwright suite depend on real OpenAI calls.
- Building a complex “judge service” or new infra unless needed.

## Principles (Operational Rules)

1. **Assert structure first; judge semantics last**
   - Structural asserts: workers spawned/not, tools called, evidence markers, completion events, UI progress indicators.
   - Semantic asserts (LLM judge): “did the user get the right answer in a helpful way?”
2. **Live tests are opt-in**
   - If `OPENAI_API_KEY` is missing, live evals/journeys should skip cleanly.
3. **Prefer deterministic models for most E2E**
   - Use `gpt-scripted` / `gpt-mock` where the goal is UI wiring, event flow, or regression-proof state assertions.
   - Use “real model” only for the small set of journey tests intended to validate UX + behavior end-to-end.

## Proposed Taxonomy (Mapped to Existing Code)

### Tier 1: Unit/Integration (Mocked LLM, real DB)
- Already present: `make test` runs backend unit tests + frontend unit tests.

### Tier 2: Hermetic evals (Stubbed LLM)
- Already present: `make eval` runs `apps/zerg/backend/evals/datasets/basic.yml` with stubs from `apps/zerg/backend/tests/conftest.py`.

### Tier 3: Live evals (Real LLM, no UI)
- Already present: `make eval-live` runs `apps/zerg/backend/evals/datasets/live.yml` with `EVAL_MODE=live`.
- Spec change: make Tier 3 cases **mostly deterministic** (tools/workers/events/artifacts), with **one** `llm_graded` assertion per case for semantics.

### Tier 4: Live E2E journeys (Real LLM + UI + Judge)
- New: add a small `*.live.spec.ts` (or similar) that:
  - drives UI via Playwright
  - collects run context (at minimum transcript + screenshot; ideally run_id + timeline)
  - calls a judge helper that returns a score + reason
  - is skipped unless explicitly enabled

## Spec: Tier 3 Improvements (Evals Live)

### Problem: “LLM-only assertions” hide root cause
Example: current `live.yml` delegation cases are judged purely by `llm_graded` (no checks that a worker actually spawned).

### Requirements
- For any “delegation” live eval:
  - assert `worker_spawned min >= 1` (or tool_called `spawn_worker`) when delegation is required
  - assert “no worker spawned” when request is ambiguous (clarification tests)
  - for `[eval:wait]` tasks, assert the run includes worker completion artifacts (or at minimum worker job reaches terminal state)

### Needed assertion plumbing
- Add `model` support to `llm_graded` assertions in YAML.
  - Today: `assert_llm_graded(..., model=...)` exists (`apps/zerg/backend/evals/asserters.py`), but the YAML schema/runner doesn’t pass it through (`apps/zerg/backend/evals/conftest.py`, `apps/zerg/backend/evals/test_eval_runner.py`).
- Add at least one “sanity guard” assertion type to prevent accidental stubbed runs in live mode.
  - Example options:
    - `not_contains: "stub-response"`
    - `total_tokens_min: 1`
    - `tools_called_min: 1` (only for cases expected to tool)

### Dataset hygiene
- Fix `apps/zerg/backend/evals/datasets/live.yml` internal counts/section headers so it’s self-consistent.
- Decide which tests are truly `critical` in live.yml (use sparingly).

## Spec: Tier 4 Live Journeys (Playwright + LLM Judge)

### What “live journey” means in this repo
Playwright already runs an isolated backend/frontend by default (`apps/zerg/e2e/playwright.config.js`) using ports 8001/8002 and schema isolation via `X-Test-Worker`.

“Live journey” adds:
- a real model for the agent involved in the journey (not `gpt-mock` / `gpt-scripted`)
- a judge that grades the final user-visible response (text-only judge; vision optional later)

### Requirements
1. **Opt-in only**
   - Skip unless an explicit env var is set (e.g. `E2E_LIVE=1`) and `OPENAI_API_KEY` exists.
2. **Minimal scope**
   - Start with 1–3 journeys total. Add more only after flake/cost are understood.
3. **Artifacts on failure**
   - Always attach:
     - final transcript (user + assistant messages)
     - a screenshot at end
     - any available backend run identifier(s)
   - If available, also fetch backend timeline:
     - `GET /api/jarvis/runs/<runId>/timeline` is already used by some tests (see `apps/zerg/e2e/tests/chat_performance_eval.spec.ts`).

### Implementation shape
- Add a helper: `apps/zerg/e2e/utils/llm-judge.ts` (or colocate under `apps/zerg/e2e/tests/helpers/`) that exposes:
  - `judgeText({ rubric, prompt, response, context? }) -> { score, reason }`
  - deterministic `response_format: json_object`
- Add `apps/zerg/e2e/tests/user-journeys.live.spec.ts` with:
  - basic UI navigation (open chat, send message, wait for completion)
  - deterministic assertions for UX (message appears, streaming finishes, no error banners)
  - a final `judgeText(...)` call for semantics

## Documentation Updates Required

1. Update `apps/zerg/e2e/TEST_SUITE_OVERVIEW.md` to reflect **Postgres schema isolation** (not SQLite).
2. Update `docs/specs/eval-dataset.md` so it matches current datasets + make targets (avoid misleading counts and “phase” status).

## Task List (Small, Shippable Chunks)

### 0) Fix doc drift (no behavior changes)
- [ ] Update `apps/zerg/e2e/TEST_SUITE_OVERVIEW.md` to reflect schema isolation via `X-Test-Worker`.
- [ ] Update `docs/specs/eval-dataset.md` (counts, live.yml behavior, current make targets).
- [ ] Normalize `apps/zerg/backend/evals/datasets/live.yml` comments/section counts (it currently disagrees with itself).

### 1) Harden eval live-mode correctness
- [ ] Add YAML support for `llm_graded.model` (plumb through pydantic + runner param mapping).
- [ ] Add a “live sanity guard” assertion type (e.g. `not_contains` or `total_tokens_min`) and use it in at least one live case.
- [ ] Convert live delegation cases to hybrid asserts:
  - [ ] add `worker_spawned min` / `tool_called spawn_worker`
  - [ ] keep exactly one `llm_graded` for semantics

### 2) Add a minimal Playwright LLM judge helper (text-only)
- [ ] Implement `judgeText()` using the existing OpenAI dependency and JSON output.
- [ ] Gate it behind `OPENAI_API_KEY` + `E2E_LIVE=1` (skip otherwise).
- [ ] Standardize judge model selection:
  - [ ] use an env var like `E2E_JUDGE_MODEL` (default to a known-good text model in your `config/models.json`)

### 3) Add 1 live journey spec (and keep it small)
- [ ] Add `apps/zerg/e2e/tests/user-journeys.live.spec.ts`:
  - [ ] journey: open `/chat`, ask a “real” question, verify streaming completes
  - [ ] capture transcript + screenshot
  - [ ] run `judgeText` with a tight rubric
- [ ] Ensure the agent used is configured to a real model for that test only.

### 4) CI wiring (opt-in)
- [ ] Decide where these run:
  - [ ] PR: no (default skip)
  - [ ] nightly / pre-deploy: yes (with secrets)
- [ ] Add/adjust make target(s) if needed (example: `make test-e2e-live`) without changing the default `make test-e2e` behavior.

## Acceptance Criteria

- Tier 3 live evals fail with **actionable** failures (worker/tool assertions pinpoint what changed).
- Tier 4 live journey can catch a “user-visible semantic regression” that deterministic asserts can’t.
- Default developer loop remains fast and stable: `make test`, `make eval`, `make test-e2e` run without OpenAI access.
