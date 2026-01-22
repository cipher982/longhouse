# Canvas Refactor & Performance Roadmap

Audience: **new frontend engineer** who jumps in mid-stream.  This document
captures the *live* status of the canvas refactor, what is already finished
and which nits are left so you can start with an up-to-date picture instead of
wading through outdated ticket threads.

> Status @ **2025-05-06**: all items originally labelled *P1–P5* have shipped
> to `main`.  The canvas now runs on a single `requestAnimationFrame` loop and
> persists its layout through the `/api/graph/layout` endpoints.  Work now
> shifts from “plumbing” to UX polish and edge-case clean-up.

---

## 1 · Quick mental model

```
                         (pointer events)
<canvas> ──► canvas_editor.rs  ──► Message enum  ──► update.rs reducer
                                          │                       │
                                          │ mark_dirty()          │
                                          ▼                       │
                                 AppState { dirty: bool, … }      │
                                          │                       │
  requestAnimationFrame loop ◄─────────────┘                       │
                         │                                        │
                         ▼                                        ▼
                    draw_nodes()                         Command / Effects
```

Invariants:

1. Only the reducer mutates `AppState`.
2. Rendering happens **exclusively** inside `ui/mod.rs::setup_animation_loop()`.

---

## 1.1 · What’s done (P1–P5)

| ID | Title | Value | Effort | Status |
|----|-------|-------|--------|--------|
| P1 | Debounce `save_state_to_api()` | ★★★★★ | ★★☆☆☆ | ✅ shipped |
| P2 | View mount → pure CSS toggle | ★★★★☆ | ★★☆☆☆ | ✅ shipped |
| P3 | Remove stray `mark_dirty()` helpers | ★★☆☆☆ | ☆☆☆☆☆ | ✅ shipped |
| P4 | `debug_log!` macro + overlay | ★★☆☆☆ | ★☆☆☆☆ | ✅ shipped |
| P5 | Batch `/api/graph/layout` persistence | ★★★★☆ | ★★★☆☆ | ✅ shipped |

Highlights that went **beyond** the original scope:

* Full `CanvasLayout` SQLAlchemy model with `UNIQUE(user_id, workspace)` and
  atomic UPSERT (`crud.upsert_canvas_layout`).
* GET `/api/graph/layout` implemented – frontend hydrates layout at startup;
  legacy `localStorage` fallback removed to expose persistence failures.
* Front-end reconciliation step upgrades *placeholder* nodes once agent data
  arrives asynchronously.
* Debug overlay draws last ~10 `debug_log!` entries in dev builds.

---

## 2 · Follow-up ideas / rough edges

These are **not blocking** but good entry-level tasks for the next sprint.

| ID | Title | Notes |
|----|-------|-------|
| F1 | Replace the last two helper-side `st.mark_dirty()` calls with `Message::MarkCanvasDirty` | In `storage.rs` (layout hydration) and `utils.rs` (`debug_log!`).  Purely for architectural cleanliness. |
| F2 | Update server layout when a canvas node is deleted | Currently stale positions linger in DB until next move.  Fire `state.state_modified=true` inside `DeleteNode` and rely on debounce, or push an immediate PATCH. |
| F3 | Configurable payload limits | Node count (≤5 000) & zoom (0.1–10 ×) are hard-coded validators.  Could move to `constants.py` + expose via `/api/config`. |
| F4 | Workspace selector | `CanvasLayout.workspace` column exists but UI has no selector yet. |
| F5 | Multi-user real-time layout collaboration | Would require WS topic `layout:{workspace}` and optimistic locking/versioning.  No groundwork yet. |

---

## 3 · File guide (for future tweaks)

* **frontend/src/ui/mod.rs** – houses the single RAF loop (`setup_animation_loop`).
* **frontend/src/update.rs** – Elm-style reducer; look at the `AnimationTick`
  handler for debounce logic.
* **frontend/src/storage.rs** – persistence helpers (`save_state_to_api`,
  `try_load_layout_from_api`).
* **backend/zerg/routers/graph_layout.py** – REST endpoints PATCH/GET.
* **backend/zerg/crud/crud.py** – `upsert_canvas_layout` & `get_canvas_layout`.
* **backend/zerg/models/models.py** – `CanvasLayout` table definition.

---

## 4 · Suggested next sprint

Week 1 F1 + F2 (code-health & stale-layout fix)
Week 2 F3 (configurable limits)
Week 3 Spike F4 (workspace selector UI + router changes)
Week 4 Architecture spike for F5 (collaboration) – write ADR, no code merge yet.

---

*Welcome aboard & happy hacking!*  The canvas core is solid – now let’s make
it delightful for users.
