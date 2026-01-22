# Message / Update / View Refactoring Roadmap

Below is a detailed roadmap for implementing the "Message / Update / View" pattern in our Rust+WASM frontend, with progress updates and next steps.

## Core Principles
- ✅ Create a central "Message" enum to represent all UI events
- ✅ Use an "update" function to change AppState
- ✅ Let view code read from AppState but never mutate it
- ✅ Dispatch messages from event handlers rather than directly manipulating state

## Progress Summary
- ✅ Created messages.rs with Message enum
- ✅ Created update.rs with central update function
- ✅ Added dispatch method to AppState
- ✅ Created views.rs with view functions
- ✅ Refactored all UI event handlers in ui/events.rs to use the new pattern
- ✅ Fixed compiler warnings
- ✅ Refactored canvas interaction code
- ✅ Refactored dashboard code
- ✅ Added ZoomCanvas message for wheel events

## Detailed Progress & Next Steps

────────────────────────────────────────────────────────────────
1. ✅ CREATE A NEW "MESSAGES.RS" FILE
────────────────────────────────────────────────────────────────
**COMPLETED**

We've created the messages.rs file with a comprehensive Message enum that includes:
- View switching (ToggleView)
- Agent management (CreateAgent, EditAgent, DeleteAgent)
- Node manipulation (UpdateNodePosition, AddNode, AddResponseNode)
- Canvas controls (ToggleAutoFit, CenterView, ClearCanvas)
- Canvas zooming (ZoomCanvas)
- Input handling (UpdateInputText)
- Dragging state (StartDragging, StopDragging)
- Canvas dragging (StartCanvasDrag, UpdateCanvasDrag, StopCanvasDrag)
- Modal operations (SaveAgentDetails, CloseAgentModal)
- Task operations (SendTaskToAgent)
- Tab switching (SwitchToMainTab, SwitchToHistoryTab)
- Auto-save operations (UpdateSystemInstructions, UpdateAgentName)

────────────────────────────────────────────────────────────────
2. ✅ ADD AN "UPDATE.RS" FILE WITH A CENTRAL UPDATE FUNCTION
────────────────────────────────────────────────────────────────
**COMPLETED**

We've created update.rs with a comprehensive update function that handles all message variants defined in the Message enum.

────────────────────────────────────────────────────────────────
3. ✅ CLEAN UP YOUR APPSTATE (STATE.RS)
────────────────────────────────────────────────────────────────
**COMPLETED**

We've added a dispatch method to AppState that:
- Takes a Message
- Calls the update function
- Handles state saving
- Triggers view rendering

All direct state manipulations have been moved to the update function.

────────────────────────────────────────────────────────────────
4. ✅ DISPATCH MESSAGES FROM EVENT HANDLERS (UI/EVENTS.RS)
────────────────────────────────────────────────────────────────
**COMPLETED**

We've refactored all event handlers in ui/events.rs to use the new pattern:
- Auto-fit toggle button
- Center view button
- Clear button
- Tab navigation (Dashboard/Canvas switching)
- Create agent button
- Modal close, save, and send buttons
- Modal tab switching
- System instructions auto-save
- Agent name auto-save

────────────────────────────────────────────────────────────────
5. ✅ REFACTOR CANVAS MOUSE EVENTS (COMPONENTS/CANVAS_EDITOR.RS)
────────────────────────────────────────────────────────────────
**COMPLETED**

We've refactored all canvas mouse events to use the Message/Update pattern:
- Mousedown event now dispatches StartDragging or StartCanvasDrag messages
- Mousemove event now dispatches UpdateNodePosition or UpdateCanvasDrag messages
- Mouseup event now dispatches StopDragging or StopCanvasDrag messages
- Wheel event now dispatches ZoomCanvas message

────────────────────────────────────────────────────────────────
6. ✅ REFACTOR DASHBOARD EVENT HANDLERS (COMPONENTS/DASHBOARD.RS)
────────────────────────────────────────────────────────────────
**COMPLETED**

We've refactored the dashboard event handlers to use the Message/Update pattern:
- Run button now dispatches SendTaskToAgent message

────────────────────────────────────────────────────────────────
7. ✅ SEPARATE YOUR "VIEW" CODE FROM THE "UPDATE" CODE
────────────────────────────────────────────────────────────────
**COMPLETED**

We've created views.rs with functions to render different parts of the UI:
- render_active_view
- render_dashboard_view
- render_canvas_view
- hide_agent_modal
- show_agent_modal

────────────────────────────────────────────────────────────────
8. ✅ DECIDE ON A RENDERING STRATEGY
────────────────────────────────────────────────────────────────
**COMPLETED**

We've implemented a "dispatch-then-render" strategy where:
1. The dispatch method returns a boolean indicating if a render is needed
2. The caller checks this boolean and then explicitly calls refresh_ui_after_state_change
3. This approach ensures we never try to borrow APP_STATE while already holding a mutable borrow

This clean separation between state updates and rendering helped solve borrowing issues while maintaining the unidirectional data flow of the pattern.

────────────────────────────────────────────────────────────────
9. ❌ OPTIONAL: INTRODUCE A "PROGRAM LOOP"
────────────────────────────────────────────────────────────────
**NOT STARTED**

We haven't implemented a program loop yet, as we're focusing on migrating existing code first.

**Next Steps:**
- Decide if a program loop would benefit the application
- If yes, implement a simple program loop that handles the update-render cycle

## Known Issues & Considerations

1. **Remaining Direct State Manipulations**: There are still a few places where we directly manipulate state:
   - Setting `is_dragging_agent` flag in the mouseup handler
   - This could be moved to a new message type in the future

2. **Canvas Rendering**: The canvas rendering code is complex but has been refactored:
   - We now use messages for all canvas interactions
   - The draw_nodes function is still called directly in some places

3. **Error Handling**: We've improved error handling in the dispatch function, but should review other areas.

4. **Testing**: We should add tests for the new message/update pattern to ensure it works correctly.

5. **Performance**: The current implementation re-renders the entire UI on any state change. We might want to optimize this for larger applications.

6. **Rust Borrowing Rules**: We encountered and fixed an issue with Rust's borrowing checker:
   - Problem: The original implementation tried to refresh the UI while still holding a mutable borrow on APP_STATE
   - Solution: Modified dispatch() to return a boolean indicating if refresh is needed instead of directly calling refresh
   - Each event handler now follows a pattern of: borrow → dispatch → drop borrow → conditionally refresh
   - This avoids "already mutably borrowed: BorrowError" panics by ensuring we never have nested borrows

## Next Priorities

1. Consider adding a dedicated message for setting the `is_dragging_agent` flag
2. Add tests for the Message/Update pattern
3. Consider implementing a program loop if needed
4. Optimize rendering to only update what's changed

## Summary of Files & Roles

• **messages.rs** ✅
  Defines the enum of possible user or system "events."
  Now includes all necessary message types for UI events.

• **update.rs** ✅
  Contains the update(AppState, Message) function.
  Calls small methods on AppState to mutate data.
  Logic for each event is centralized here.

• **state.rs** ✅
  Contains AppState itself—the single source of truth.
  Also includes small helper methods for domain logic.
  Added dispatch method to handle messages.

• **views.rs** ✅
  Contains view functions that render the UI based on state.
  Functions like render_dashboard_view and render_canvas_view.
  They read from state to build HTML or render canvas.
  They never mutate the global state directly.

• **ui/events.rs** ✅
  Fully refactored to dispatch messages instead of mutating state directly.

• **components/canvas_editor.rs** ✅
  Fully refactored to dispatch messages for all mouse events.

• **components/dashboard.rs** ✅
  Fully refactored to dispatch messages for agent interactions.

This unidirectional approach simplifies debugging, reduces confusion about who's mutating state, and helps reason about concurrency and performance. Whenever something happens in the UI, it goes through a clearly defined path:

(1) Construct Message
(2) update(state, message)
(3) Re-render views reading from state
