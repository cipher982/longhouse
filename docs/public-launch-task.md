# Public Launch Task Doc (Must Follow)

Date: 2026-01-30
Owner: David Rose
Status: In progress

## Purpose
This is the execution checklist and plan for improving the app experience before public launch.
We must follow this doc for scope, finish conditions, and validation steps.

## Context Summary (So Far)
- Product vision: Zerg is a unified, lossless agent-session timeline across providers; the timeline is the product.
- Current UX is powerful but crowded for a first public release (many tabs, unclear primary surface).
- Prior art patterns we want to emulate:
  - Trace-first UX (timeline as the core artifact).
  - List -> detail trace views with structure and metrics.
  - Filters and metadata as first-class UI.
  - Waterfall/timeline mental model for tool calls.

## Goals (Explicit)
1) Time-to-value: a new user sees their first session in < 2 minutes on macOS.
2) Zero-key experience: UI boots and demo timeline works with no API key.
3) Clear product surface: Sessions/Timeline is the primary place users land.
4) Docs clarity: README passes a 10-second test (value prop + screenshot + install paths).
5) Reliability: a single onboarding smoke check validates a fresh setup path.

## Finish Conditions (Required)
- [x] App landing or first-run experience clearly points to Timeline as the product.
- [x] New users can see demo sessions without API keys.
- [ ] README rewritten to highlight timeline-first value and 3 install paths.
- [x] Onboarding smoke command exists and passes locally.
- [x] README onboarding contract is validated in CI (docs-as-source).
- [x] Onboarding funnel passes locally from a fresh clone with no hidden env flags.
- [x] CI "Onboarding Funnel" job is green on a clean push (contract + UI selectors).
- [ ] Core UI smoke snapshots pass (qa-ui-smoke).
- [ ] Shipper smoke test passes (if shipper path is enabled in the flow).

## Scope (Near-term)
We focus on UX improvements + onboarding clarity. No deep backend refactors unless strictly required.

## Plan

### Phase 0: Align on primary surface
- [x] Decide primary nav and default route (Timeline vs Dashboard vs Chat).
- [x] Decide naming: "Sessions" -> "Timeline" (recommended).
- [x] Define the default landing behavior for auth-enabled and auth-disabled modes.

### Phase 1: First-run / empty-state UX
- [x] Implement guided empty state for Timeline with 3 steps:
  1) Connect shipper (optional)
  2) Run demo session (no keys)
  3) Explore timeline
- [x] Add a lightweight demo seed path for sessions if none exist.
- [ ] Add CTA from Chat to “View session trace” after a run.

### Phase 2: Timeline polish
- Improve Session detail header (goal, repo/project, duration, status).
- Add basic metrics (tool count, duration, latency if available).
- Add filters within detail view (user/assistant/tool) and search.

### Phase 3: Docs + onboarding
- [ ] Rewrite README to center the timeline value prop.
- [ ] Provide 3 install paths (quick, guided, developer).
- [ ] Add screenshots and a short “what you get” section.
- [x] Add a single onboarding smoke command to verify first-run.
- [x] Add README “onboarding contract” block and CI runner (docs-as-source).

## Current State (Summary)
- Timeline is now the primary nav item and default route for authenticated/dev users.
- `/sessions` routes redirect/alias to `/timeline` (detail view remains supported).
- Guided Timeline empty state added with demo seed + optional shipper CTA.
- Demo sessions seeded via `POST /api/agents/demo` (idempotent).
- Onboarding smoke target added: `make onboarding-smoke` (not yet run).
- Onboarding funnel CI job added to `contract-first-ci.yml`.
- Onboarding funnel runs steps in the user’s shell (avoids Node version mismatch).
- Onboarding Playwright test searches upward for the README contract and accepts multiple demo session cards.
- Onboarding funnel passes locally from a fresh clone.

## Tests / Validation (Existing)
Use these to prove we didn’t regress the experience:
- `make env-check` (required env sanity)
- `make doctor` (dev stack diagnostics)
- `scripts/validate-setup.sh` (setup validation)
- `make qa-ui-smoke` (core UI snapshots)
- `make test-e2e-core` (core flows)
- `make shipper-smoke-test` (shipper live smoke; if used)
- `make test-install-runner` (runner install script tests)
- `make onboarding-funnel` (docs-as-source funnel)

## Docs-as-Source CI (Onboarding Funnel)
- README contains an “onboarding contract” block (JSON).
- CI runner extracts this block and executes its commands in a temp workspace.
- Playwright checks use the contract’s selectors/labels; UI/doc drift fails CI.
- No hidden env flags; all behavior is declared in the contract block.
- Contract owns any onboarding-specific env tweaks (e.g., POSTGRES_DATA_PATH for isolated DB).
- If the landing page/CTA changes, selectors in the contract must be updated or CI fails.

## Gaps / Missing Tests
- No automated test for a full onboarding “happy path”.
- Docs validation is limited to the onboarding contract (README rewrite not yet enforced).
- No packaging smoke test for a future `brew install zerg` or `install.sh`.

## Decisions (Locked)
- **Primary surface = Timeline.** The Sessions list is the product; rename it to “Timeline.”
- **Default route for authenticated users = Timeline.** Root `/` remains a public landing page, but post-auth and dev auth-disabled flows land on Timeline.
- **Shipper onboarding is optional, not required.** First-run flow includes “Connect shipper” as a recommended step, but demo sessions/timeline must work without it or API keys.

## Working Agreement
- Do not ship to public without completing the Finish Conditions.
- Update this doc if we discover new blockers or constraints.
