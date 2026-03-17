# Insights Tightening

Status: Shipped, later partially superseded by `docs/specs/continuity-memory-boundary.md`

## Executive Summary

Insights still have real value as a continuity primitive, but the current feature shape is too loose. The useful part is the stored insight corpus plus machine/browser read paths. The unhealthy part is unattended reflection writing generic rows and proposals that nobody reviews.

This pass keeps the thin insight primitive and restores machine-readable access without undoing the browser/machine auth split. It also pauses automatic reflection by default so we stop generating more low-signal rows before launch.

Historical note: this doc covers the earlier auth + auto-reflection tightening pass only. A later continuity-boundary cleanup added the minimal browser `/insights` curation page and moved ops alerts into `OperationalIncident`; use `docs/specs/continuity-memory-boundary.md` for the current product shape.

## Decisions

### Decision: Keep insights, pause auto-reflection
**Context:** Live hosted data shows a mix of high-value operational learnings and templated reflection sludge.
**Choice:** Keep manual/system insight writes, but disable the scheduled reflection job by default.
**Rationale:** This preserves the useful continuity layer and stops new proposal/insight noise immediately.
**Revisit if:** We add stronger filtering, provenance, and a real consumption workflow.

### Decision: Fix MCP with a machine-owned read route
**Context:** `query_insights` currently calls the browser-authenticated `/api/insights` route, so machine sessions get `401`.
**Choice:** Add a machine-authenticated `/api/agents/insights` read route and point MCP at it.
**Rationale:** This restores machine access without reintroducing mixed-auth reads on the browser route.
**Revisit if:** We later consolidate machine continuity reads behind a different namespace.

### Decision: Keep the implementation bounded
**Context:** Pre-launch cleanup should favor obvious wins over broad redesign.
**Choice:** Do not redesign the reflection model, proposals schema, or insights storage in this pass.
**Rationale:** The immediate launch issues are broken machine reads, misleading docs/copy, and unattended low-quality writes.
**Revisit if:** We choose to revive reflection as a first-class feature later.

## Scope

In scope:
- Add `/api/agents/insights` as the machine-authenticated read path
- Repoint MCP `query_insights` to the machine route
- Add focused tests for browser vs machine insight reads and MCP query behavior
- Disable scheduled reflection by default
- Update TODO/docs/copy so insights/proposals are described honestly

Out of scope:
- Redesigning the reflection prompt
- Deleting the proposals table or routes
- Adding a new dedicated Insights page
- Reworking manual `POST /api/insights`
- Moving alert-style rows into a separate table

## Acceptance Criteria

1. Browser reads remain on `/api/insights` and still require a browser session cookie.
2. Machine reads succeed on `/api/agents/insights` with machine auth.
3. MCP `query_insights` uses the machine route and no longer depends on browser auth.
4. Scheduled reflection does not auto-run on a default instance.
5. Stale docs/UI copy no longer claim there is an active first-class Insights page or that reflection runs every 6 hours by default.

## Implementation Notes

- Keep the browser route behavior from the auth-boundary cleanup intact.
- Keep `POST /api/insights` on the machine-authenticated path.
- Manual reflection endpoints can stay available for explicit use even while the cron job is paused.
