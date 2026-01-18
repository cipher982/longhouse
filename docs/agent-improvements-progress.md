# Agent Improvements Progress

## Goals
- Preserve **progressive disclosure** for worker evidence (no auto-mounting).
- Keep the supervisor loop minimal while enabling on-demand access to raw artifacts.
- Reduce context bloat safely (later phases).

## Decisions
- Evidence markers like `[EVIDENCE:run_id=...,job_id=...,worker_id=...]` remain **pointers**.
- We will add a **tool** to dereference evidence on demand instead of auto-expanding.
- Implementation will be phased to keep risk low.

## Phases & Status

### Phase 0 — Tracking + Alignment
- [x] Create this progress doc
- [x] Document decisions

### Phase 1 — Progressive Evidence Tool
- [x] Add `get_worker_evidence(job_id, budget_bytes=...)` tool (uses EvidenceCompiler)
- [x] Ensure tool is discoverable (core tool or via search_tools)
- [x] Update supervisor prompt to mention evidence marker + tool usage
- [x] Basic test/validation

### Phase 2 — Deterministic Context Budget
- [ ] Add pre-LLM trimming step (system + last N turns + recent tool results)
- [ ] Configurable via env
- [ ] Guardrails to keep tool-call pairs intact

### Phase 3 — Large Tool Outputs by Reference
- [ ] Store large tool outputs and return marker + preview
- [ ] Add tool to fetch full output by marker

### Phase 4 — Optional `done()` Tool
- [ ] Add `done()` tool (telemetry signal only)
- [ ] Track usage in run metadata / logs

## Status Log
- 2026-01-18: Initialized plan and progress tracking doc.
- 2026-01-18: Phase 1 implemented (progressive evidence tool + prompt update).
- 2026-01-18: Added automated tests for evidence compiler + supervisor tool; `make test MINIMAL=1` passed.
