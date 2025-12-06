# Canvas UX Phase-2 – Progress PRD

_File maintained during the implementation sprint to track which items from the
original spec have shipped, started, or are still pending._

Last updated: 2025-06-15

---

## 1. Scope Recap

Phase-2 covers the following high-level themes:

1. Declutter the Canvas toolbar – replace Auto-Fit toggle with a single
   “Find Everything” action and move **Clear Canvas** into a safer location.
2. Provide a smooth camera animation when centring / resetting the view.
3. Make layout persistence reliable, including an offline/localStorage
   fallback so work isn’t lost when the backend is unreachable.
4. Introduce **Workflows** (multiple canvases) managed via a
   worksheet-style tab bar.
5. Keyboard & interaction polish (shortcuts, Space-drag pan, etc.).

---

## 2. Checklist & Status

| #   | Feature / Task                                         | Status        | Notes                                                         |
| --- | ------------------------------------------------------ | ------------- | ------------------------------------------------------------- |
| 1   | Remove Auto-Fit toggle, add **Find Everything** button | ✅ **Done**   | `ui/main.rs` now renders single `#center-view` button.        |
| 2   | Style toolbar button                                   | ✅ **Done**   | `.toolbar-btn` CSS added.                                     |
| 3   | Hidden **Clear Canvas** inside ⋮ menu                  | ✅ **Done**   | Uses `<details>` dropdown; confirm dialog kept.               |
| 4   | Smooth centre / fit animation                          | ✅ **Done**   | `AppState::center_view()` uses `animate_viewport`.            |
| 5   | Keyboard shortcuts (F = fit, 0 = reset)                | ✅ **Done**   | Listener in `ui/events.rs`.                                   |
| 6   | Reset-view (origin, 100 %)                             | ✅ **Done**   | `Message::ResetView`, `AppState::reset_view()`.               |
| 7   | Offline auto-save fallback                             | ✅ **Done**   | LocalStorage save+hydrate + status banner.                    |
| 8   | Hold-Space canvas pan                                  | ✅ **Done**   | Key listener + cursor implemented; uses body.space-pan class. |
| 9   | Workflow tab bar UI                                    | ✅ **Done**   | Tab bar component renders; create & switch wired.             |
| 10  | Backend workflow list / rename / delete APIs           | ✅ **Done**   | CRUD helpers + routes added and tested.                       |
| 11  | `/graph/layout?workflow_id=` support + DB migration    | ✅ **Done**   | Model column, CRUD & router updated; DB migration complete.   |
| 12  | Undo / Redo stack                                      | ⬜ **Future** | Moved to future enhancement backlog.                          |

Legend: ✅ completed 🟡 in progress ⬜ not started

---

## 3. Next Immediate Steps (agreed 2025-06-07)

1. Implement **Hold-Space** panning mode in the Canvas renderer.
2. Build first iteration of **Workflow Tab Bar** (front-end only, mocked
   backend IDs for now). Includes Create, Select, simple Rename.
3. Extend layout save/load helpers to accept `workflow_id` once the backend
   supports it.

Backend work (parallel):

1. Finish `GET /workflows` route and make POST return persisted ID.
2. Add `workflow_id` column & query param to canvas layout endpoints.

---

## 4. Notes & Decisions Log

- 2025-06-07 — Agreed on worksheet-style tab bar for workflows, similar to
  Snowflake worksheets or browser tabs. One canvas per workflow; switching
  tabs auto-saves the current layout.

- 2025-06-07 — Offline fallback reinstated but emits orange banner so server
  issues are visible.

---

Maintainer: `@openai-codex` • Feel free to append new decisions below.
