# Feature Request: Drag-and-Drop Agent Shelf → Canvas

## Context

- **Current Implementation:**
  - The agent shelf (sidebar) lists all agents as clickable pills. Clicking a pill adds the agent as a node to the canvas, always centered.
  - The canvas supports dragging nodes already placed, but does **not** support drag-and-drop from the shelf.
  - There is no visual feedback for dragging from the shelf or dropping onto the canvas.
  - All drag logic is internal to the canvas; the shelf is a simple clickable list.
  - No drag-and-drop affordances or styles exist in the CSS.

- **Relevant Files:**
  - `frontend/src/components/agent_shelf.rs` — Renders the agent shelf and pills, currently click-to-add only.
  - `frontend/src/components/canvas_editor.rs` — Handles canvas rendering and node drag logic (internal only).
  - `frontend/src/pages/canvas.rs` — Mounts/unmounts the canvas and shelf.
  - `frontend/src/state.rs`, `frontend/src/messages.rs` — State and message definitions for node addition and drag events.
  - `frontend/www/styles.css` — Styles for shelf, pills, and canvas.

## Goals

- **Enable users to drag agent pills from the shelf and drop them anywhere on the canvas.**
- **Provide clear visual feedback** during drag (e.g., pill highlights, canvas drop target highlight).
- **Node should appear at the drop location** (not always centered).
- **Maintain accessibility:** fallback to click-to-add for keyboard users.
- **Follow modern web UI best practices** for drag-and-drop.

## Key Findings

- No drag-and-drop logic currently exists for shelf → canvas.
- All drag state is internal to the canvas; shelf is click-only.
- The message system (`AddAgentNode`) and state are robust and can be reused for this feature.
- CSS does not provide drag/drop affordances.

## Implementation Plan

### 1. Agent Shelf Changes
- Make each `.agent-pill` draggable (`draggable="true"`).
- On `dragstart`, set the agent ID/name in the drag event's data.
- Add a `.dragging` CSS class for visual feedback.

### 2. Canvas Changes
- Listen for `dragover` and `drop` events on the canvas container (`#canvas-container` or `#node-canvas`).
- On `dragover`, prevent default and add a `.canvas-drop-target` class for highlight.
- On `drop`, read the agent ID/name from the drag event, get the mouse position relative to the canvas, and dispatch `Message::AddAgentNode` at that position.

### 3. State/Message Changes
- Reuse `AddAgentNode` message, but pass the correct coordinates from the drop event.
- Optionally, add messages for `AgentDragStarted` and `AgentDragEnded` for advanced feedback.

### 4. CSS
- Add `.agent-pill.dragging` for the pill being dragged.
- Add `.canvas-drop-target` for the canvas drop highlight.

### 5. Accessibility
- Ensure click-to-add remains as a fallback for keyboard users.
- Consider ARIA attributes for drag-and-drop.

## Example User Flow
1. User drags an agent pill from the shelf.
2. Canvas highlights as a drop target.
3. User drops the pill at a desired location.
4. A new agent node appears at the drop location.
5. (Optional) Click-to-add remains available for accessibility.

## References
- [MDN Drag and Drop API](https://developer.mozilla.org/en-US/docs/Web/API/HTML_Drag_and_Drop_API)
- [Accessible Drag and Drop](https://www.smashingmagazine.com/2021/07/accessible-drag-drop/)

---

**This feature will modernize the canvas UX, align with user expectations, and make multi-agent workflows more intuitive.**
