Certainly! Here’s a detailed markdown report you can hand off to a developer:

---

# Refactor Proposal: Decouple Agent and Node Logic in Frontend

## Background

The current frontend codebase for the Zerg project has legacy coupling between **Agents** (backend entities with names, instructions, etc.) and **Nodes** (visual elements on the canvas, used for workflows). This coupling is a remnant of an earlier design where all agent data was managed through canvas nodes. As the application evolved, agents became first-class backend entities, but the frontend modal and update logic still rely on node-centric patterns.

This has led to bugs, most notably:
- **Editing an agent from the dashboard opens a modal with empty fields, and saving can overwrite the agent’s name with an empty string.**
- The modal’s data population logic is inconsistent, sometimes using node data, sometimes agent data, and sometimes falling back to defaults.

## Problem Statement

- **Agent editing should be agent-centric, not node-centric.**
- The modal for editing agents should always use the agent as the source of truth, regardless of whether a node exists for that agent on the canvas.
- Node logic should be strictly for visual/canvas purposes and should not affect agent data except where explicitly intended (e.g., linking a node to an agent for workflow purposes).

## Goals

- **Completely decouple agent and node logic in the frontend.**
- Ensure the Agent Configuration Modal always loads and saves data from/to the agent entity, not from/to a node.
- Make the codebase easier to maintain, reason about, and extend.

---

## Scope of Work

### 1. Refactor Agent Modal Logic

- **AgentConfigModal** should be refactored to operate solely on `agent_id`.
    - When opening the modal, always fetch agent data from `state.agents[agent_id]`.
    - Remove all logic that attempts to fetch agent data from a node or uses a `node_id` as a proxy for agent identity.
    - The modal should not require or use a `node_id` at all for agent editing.
    - All fields (name, system instructions, task instructions, schedule, etc.) should be prepopulated from the agent entity.

### 2. Update Modal Triggers

- All UI elements that open the Agent Configuration Modal (dashboard edit buttons, etc.) should pass the `agent_id` directly.
- Remove the use of synthetic node IDs (e.g., `"agent-1"`) for modal operations.

### 3. Update Save Logic

- When saving agent details from the modal, update only the agent in `state.agents` and send the update to the backend.
- Do not update any node properties (such as `node.text`) as part of agent editing.
- If the agent name field is empty, prevent saving or fallback to the existing agent name (do not allow overwriting with an empty string).

### 4. Node Logic

- Nodes should remain as visual/canvas elements.
- Nodes may have an `agent_id` field to indicate linkage to an agent, but their properties (like `text`) are for display only.
- Node creation (e.g., drag-and-drop from agent shelf) should copy the agent’s name at the time of creation, but subsequent changes to the agent should not automatically update the node, unless explicitly desired (see below).

### 5. (Optional) Define Sync Policy

- Decide on a policy for syncing agent name changes to linked nodes:
    - **Option A (Recommended):** No automatic sync. Node labels are independent after creation.
    - **Option B:** One-way sync from agent to node (requires additional logic).
    - **Option C:** Two-way sync (not recommended due to complexity).

### 6. Clean Up Legacy Code

- Remove or refactor messages and functions that sync nodes and agents (`SyncNodeToAgent`, `SyncAgentToNode`) unless explicitly needed for workflow features.
- Remove fallback logic in the modal and update functions that tries to use node data for agent editing.

---

## Acceptance Criteria

- Editing an agent from the dashboard always shows the correct agent data in the modal.
- Saving agent changes never overwrites agent data with empty or default values due to missing node data.
- Canvas nodes are unaffected by agent edits unless a specific sync action is triggered.
- The codebase has a clear separation between agent management and node/canvas management.

---

## Suggested File/Function Targets

- `frontend/src/components/agent_config_modal.rs` (main modal logic)
- `frontend/src/update.rs` (message handling, especially `SaveAgentDetails`, `EditAgent`)
- `frontend/src/views.rs` (modal display logic)
- Any UI code that triggers agent editing (dashboard, agent shelf, etc.)

---

## Risks & Considerations

- This is a breaking change for any features that currently rely on node-agent coupling for agent editing.
- Ensure all UI entry points for agent editing are updated to use the new agent-centric modal logic.
- Test thoroughly for regressions in both agent editing and canvas/node workflows.

---

## Summary

This refactor will modernize the frontend architecture, eliminate a class of bugs, and make future development more robust and maintainable. The modal and agent editing logic will become simpler and more reliable, and the distinction between backend entities (agents) and frontend visuals (nodes) will be clear and enforceable.

---

## Refactor Checklist (in-progress)

Use this list to track the actual implementation work.  Tick each task (`[x]`) once it is complete and merged into the main branch.  Do **not** skip steps – each builds on the previous one.

### 1  Data model & naming

- [x] Rename `Node` struct to `CanvasNode` (or equivalent) in `frontend/src/models.rs` and add a temporary type-alias for backward compatibility.
- [x] Remove helper methods on the struct that read/modify agent data (`history()`, `status()`, `get_status_from_agents`, etc.) and delete the now-redundant `NodeAgentLegacyExt` shim.

### 2  Frontend message API

- [x] Delete `SyncNodeToAgent` and `SyncAgentToNode` message variants and their handlers.
- [x] Rename `AddAgentNode` → `AddCanvasNode` (or similar) and adjust all call-sites (`agent_shelf.rs`, `ui/events.rs`, `canvas_editor.rs`, tests).
- [ ] Ensure `EditAgent(u32)` remains the sole agent-editing entrypoint.
- [x] Ensure `EditAgent(u32)` is now the sole agent-editing entrypoint (modal opens directly by agent_id).

### 3  Agent Config Modal

- [x] Change public signature to `AgentConfigModal::open(document, agent_id: u32)`.
- [x] Removed logic that operated on `node_id` / synthetic ids; modal pulls data solely from `state.agents`.
- [x] Modal now stores `data-agent-id` instead of `data-node-id`.

### 4  View helpers

- [x] `views::show_agent_modal` updated to accept `agent_id` (u32).
- [x] `Message::EditAgent` handler simplified – direct modal open, node look-ups removed.

### 5  Dashboard & other UI entry points

- [ ] Audit all modal triggers; ensure they dispatch `Message::EditAgent(id)` directly.
- [ ] Remove obsolete code paths that open by node id (context menus, etc.).

### 6  Canvas-only logic

- [ ] Adjust node creation flows so they pass `Some(agent_id)` into `AddCanvasNode` but never attempt to synchronise back.
- [ ] Implement optional “RefreshCanvasLabels” command if we decide to keep manual sync from agents → nodes.

### 7  Remove legacy helpers

- [x] Deleted `frontend/src/node_agent_legacy_ext.rs` and all call-sites (`_set_id`, `set_status`, `task_instructions`, etc.)
- [ ] Delete functions that parse `"agent-{id}"` from node ids (still referenced in a few helpers).
- [ ] Drop unused struct fields / helpers after the coupling is removed.

### 8  Tests & linting

- [ ] Update frontend tests for the new message names and modal API.
- [ ] Run `./frontend/run_frontend_tests.sh` and `./backend/run_backend_tests.sh` until both suites pass.
- [ ] Run `pre-commit` (if configured) and fix any newly reported lint errors.

### 9  Documentation

- [ ] Add a “Canvas Nodes vs. Agents” explanatory section to `README.md`.
- [ ] Remove or rewrite outdated references in other docs.

> **Tip for reviewers:** please keep each PR narrowly focused on a single checklist bullet (or a tight group of related bullets) to ease code review and avoid merge conflicts.

---

## Dev-notes / Things learned so far

* The original `Node` struct is referenced **~150 times** across the
  frontend.  Introducing `CanvasNode` with a temporary type-alias means we
  can land the rename without touching every call-site at once.  The alias
  will be removed in a later checklist item once the migration is complete.

* `SyncNodeToAgent` / `SyncAgentToNode` turned out to be *dead code* in
  most of the UI – removing them simplified `update.rs` and caught a few
  hidden borrow-checker work-arounds.

* Renaming `AddAgentNode` required updates in `ui/events.rs`,
  `canvas_editor.rs`, and the agent shelf component.  Tests referenced the
  old variant as well – those will be updated in a dedicated “tests” pass.

* While inspecting the animation tick path we noticed `Node::get_status_from_agents` – this method is one of the helpers slated for deletion.  We’ll
  adjust the status lookup to query `state.agents` directly when we remove
  the helper in the next task.



**Contact:**
For questions or clarifications, please reach out to the project maintainer or the original author of this report.
