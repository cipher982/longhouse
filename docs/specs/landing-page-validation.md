# Landing Page Visual Validation Spec

**Created:** 2026-01-01
**Status:** In Progress
**Protocol:** SDP-1

---

## Executive Summary

Validate the landing page at `/landing` against the comprehensive checklist in `docs/work/SCREENSHOT_CAPTURE_TASK.md`. Use Playwright for screenshot capture and Claude's vision capabilities to verify each section.

---

## Decision Log

| Decision | Context | Choice | Rationale |
|----------|---------|--------|-----------|
| Screenshot resolution | Need consistent capture | 1440px width | Desktop viewport, shows full sections |
| Full-page vs sections | Could do one big screenshot | Section-by-section | Better for detailed validation |
| Playwright snapshot vs screenshot | Could use accessibility tree | Screenshots | Vision validation requires actual images |

---

## Phase Definitions

### Phase 1: Screenshot Capture
**Goal:** Capture screenshots of each landing page section using Playwright.

**Acceptance Criteria:**
- [ ] Dev server is running at localhost:30080
- [ ] Navigate to `/landing` (not `/` which redirects to dashboard in dev)
- [ ] Capture hero section screenshot
- [ ] Capture PAS section screenshot
- [ ] Capture scenarios section screenshot
- [ ] Capture demo section screenshot
- [ ] Capture differentiation section screenshot
- [ ] Capture nerd section screenshot
- [ ] Capture integrations section screenshot
- [ ] Capture FAQ section screenshot
- [ ] Capture footer screenshot

**Test Commands:**
```bash
# Verify dev server
curl -s http://localhost:30080/api/health | jq .status
```

### Phase 2: Visual Validation
**Goal:** Use vision capabilities to verify each section against checklist.

**Acceptance Criteria:**
- [ ] Hero: Robot mascot visible, gradient text, CTAs work
- [ ] PAS: Problem/solution copy accurate, no placeholders
- [ ] Scenarios: Three cards with illustrations (Health, Inbox, Home)
- [ ] Demo: macOS chrome (traffic lights), "Coming Soon" placeholder
- [ ] Differentiation: Comparison table, Swarmlet checkmarks
- [ ] Nerd: GPT-5.2/5 Mini mention, canvas screenshot, traffic lights
- [ ] Integrations: Icons render, "Soon" badges present
- [ ] FAQ: Expandable questions, OpenAI mention
- [ ] Footer: Links present, current copyright year

### Phase 3: Build Verification
**Goal:** Ensure frontend builds without errors.

**Acceptance Criteria:**
- [ ] `cd apps/zerg/frontend-web && bun run build` completes successfully
- [ ] No TypeScript errors
- [ ] No missing assets

**Test Commands:**
```bash
cd apps/zerg/frontend-web && bun run build
```

---

## Implementation Status

| Phase | Status | Notes |
|-------|--------|-------|
| Phase 1 | Pending | |
| Phase 2 | Pending | |
| Phase 3 | Pending | |

---

## Validation Results

_To be filled during Phase 2_

### Hero Section
- Status:
- Notes:

### PAS Section
- Status:
- Notes:

### Scenarios Section
- Status:
- Notes:

### Demo Section
- Status:
- Notes:

### Differentiation Section
- Status:
- Notes:

### Nerd Section
- Status:
- Notes:

### Integrations Section
- Status:
- Notes:

### FAQ Section
- Status:
- Notes:

### Footer
- Status:
- Notes:

---

## Definition of Done

- [ ] All Phase 1 screenshots captured
- [ ] All Phase 2 visual validations pass
- [ ] Phase 3 build passes
- [ ] Codex MCP final review approves
- [ ] Spec updated with results
