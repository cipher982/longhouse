# Docs Refresh (2025-12-28)

This is a documentation refresh pass to bring “how to run / how it works” docs back in sync with current code and recent changes.

## Baseline + Commit Review Window

Baseline commit (last repo-level doc sweep marker):

- `f198a2c` (2025-12-27) — phase 5: document frontend logging modes in `AGENTS.md`

Commits reviewed (baseline → `HEAD`), focusing on doc-impacting changes:

- `626adf5` phase 5: mark spec as complete with implementation summary
- `e962e57` mark spec as Implemented after final review
- `1566a68` feat(db): add correlation_id to agent_runs for request tracing
- `6a56b9d` fix(docker): add TAVILY_API_KEY to backend environment
- `ecbf429` feat(backend): add supervisor tool SSE event schema and frontend event types
- `49912f0` fix(backend): add SSE subscriptions for supervisor tool events
- `0015f35` feat(frontend): add supervisor tool store and UI components
- `0effeeb` fix(jarvis): render tool cards inline with messages for temporal ordering

## What Changed (High Signal)

- Updated “core entrypoint” docs to match current compose + Makefile behavior (ports, commands, prod nginx config).
- Updated specs to include new SSE event types (`supervisor_tool_*`) and corrected “persistence” claims for ToolCards (session-scoped today).
- Cleaned up E2E docs to match the current Playwright harness and Bun-first workflow.
- Fixed a few “source-of-truth” mismatches (e.g., `apps/zerg/e2e/playwright.config.js` using `npm` instead of `bun`).

## Known Gaps (Explicit)

- Supervisor ToolCards are session-scoped today (cleared on conversation switch). DB persistence/rehydration is a separate phase.
- `apps/zerg/e2e/tests/supervisor-tool-visibility.spec.ts` exists but is skipped because it relies on dev-only event injection.
