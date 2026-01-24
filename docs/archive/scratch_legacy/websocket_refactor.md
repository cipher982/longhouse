# WebSocket Refactoring Plan: Single Connection + Event-Driven Architecture

## Status: Backend Complete ✓, Frontend Infrastructure Complete ✓, View Integration In Progress 🚀
Last Updated: April 9, 2024

## 1. Original Problem & Goals [✓]

**Observation:** Logs show frequent socket openings/closings, particularly when moving from a "Dashboard" view to a "Chat" view. This repeated setup and teardown leads to:
- Socket churn (overhead in opening new connections)
- Potential state-sync issues (missed or out-of-order messages)
- Higher complexity (frontend code with multiple WebSocket endpoints and event handlers)

**Goal:** Replace the multiple-socket approach with a single, persistent WebSocket for the application lifecycle, plus a streamlined method to manage real-time events.

## 2. Implementation Progress

### Backend Completed ✓
1. Created event bus infrastructure:
   - `EventType` enum for standardized event types
   - `EventBus` class with publish/subscribe functionality
   - Global event bus instance
   - Full test coverage for event bus functionality

2. Implemented event publishing for agent operations:
   - Created `@publish_event` decorator for clean event emission
   - Added events for agent CRUD operations (created, updated, deleted)
   - Added comprehensive tests for agent events
   - Kept existing functionality intact while adding events

3. Created new connection manager:
   - Implemented `TopicConnectionManager` in `new_manager.py`
   - Added topic-based subscription system
   - Integrated with event bus for agent and thread events
   - Added comprehensive unit tests in `test_topic_manager.py`

4. Created new message handlers:
   - Implemented handlers in `new_handlers.py` for subscribe/unsubscribe
   - Added support for initial topic subscriptions
   - Integrated with existing message types (ping/pong)
   - Added error handling for invalid topics/resources

5. Added new WebSocket endpoint:
   - Created `/api/ws/v2` endpoint in `new_websocket.py`
   - Supports topic-based subscriptions
   - Maintains backward compatibility (old endpoint still works)
   - Added integration tests in `test_new_websocket.py`

### Frontend Implementation Progress

#### Completed Core Infrastructure ✓
1. **Event Types Module** (`event_types.rs`):
   - Mirrors backend's event and message types
   - Provides type-safe enums for all message types
   - Includes topic formatting helpers
   - Full test coverage

2. **Message System** (`messages.rs`):
   - Structured message types matching backend schema
   - Serialization/deserialization support
   - Message builders for common operations
   - Message parsing and type inference
   - Comprehensive test suite
   - Added structs for backend event data payloads (e.g., `AgentEventData`, `ThreadCreatedEventData`).

3. **WebSocket Client v2** (`ws_client_v2.rs`):
   - Single connection management
   - Automatic reconnection with exponential backoff
   - Ping/pong keep-alive mechanism
   - Type-safe message handling
   - Connection state management
   - Error handling and logging
   - Refactored to use a callback pattern (`on_connect`, `on_message`, `on_disconnect`) for better integration.

4. **Topic Manager** (`topic_manager.rs`) [NEW ✓]:
   - Manages topic subscriptions (`agent:*`, `thread:*`).
   - Sends `subscribe`/`unsubscribe` messages to backend.
   - Handles automatic resubscription on reconnect (`resubscribe_all_topics`).
   - Routes incoming messages based on inferred topic to registered handlers.

5. **Global Instance Management** [NEW ✓]:
   - Integrated `WsClientV2` and `TopicManager` into global `AppState` using `Rc<RefCell<>>`.
   - Initialized instances and connected WebSocket in `lib.rs::start()`.
   - Linked `WsClientV2` callbacks to `TopicManager` methods.

#### View Integration Progress [NEW] 🚀

1. **Dashboard Component** [Completed ✓]:
   - Created `DashboardWsManager` to handle WebSocket lifecycle and subscriptions
   - Implemented proper cleanup on component unmount
   - Added real-time agent event handling (`agent_created`, `agent_updated`, `agent_deleted`)
   - Integrated with existing state management via `dispatch_global_message`
   - Correctly uses global `WsClientV2` and `TopicManager` instances from `AppState`

2. **Key Implementation Decisions**:
   - Component-specific WebSocket managers focus purely on subscription lifecycle management
   - Managers use global `WsClientV2` and `TopicManager` instances from `AppState`
   - Thread-local singleton pattern for manager instances (`DASHBOARD_WS`)
   - Automatic cleanup with specific handler unsubscription
   - Reuse of existing API refresh mechanisms for state updates

3. **Learnings from Dashboard Integration**:
   - Global instances in `AppState` (`WsClientV2`, `TopicManager`) are central to the architecture
   - Component managers should not create their own WebSocket connections
   - Specific handler unsubscription via `unsubscribe_handler` prevents resource leaks
   - Using traits (`ITopicManager`) enables effective testing with mocks
   - Thread-local storage works well for component-scoped instances

#### Current Challenges Addressed / Resolved ✓
1. **Message Handler Lifetime**: Addressed by using `Rc<RefCell<dyn FnMut(...)>>` for handlers in `TopicManager` and proper unsubscription in component cleanup. Components manage their subscription lifecycle through their manager's `initialize` and `cleanup` methods.

2. **Reconnection Edge Cases**: Addressed automatic topic resubscription via `TopicManager::resubscribe_all_topics` triggered by `WsClientV2`'s `on_connect` callback. State recovery within components will be handled during view integration.

3. **Component Integration Pattern** [NEW]: Established pattern using component-specific WebSocket managers that handle:
   - Subscription lifecycle with global `TopicManager` (`initialize`/`cleanup`)
   - Topic-specific message handling
   - Event handling and state updates
   - Proper cleanup with specific handler unsubscription

## 3. Next Immediate Steps [Updated]

1. **View Integration** [In Progress]:
   - ✓ Create component-specific WebSocket manager pattern
   - ✓ Implement Dashboard real-time updates
   - Next: Chat View Integration
     - Create `ChatViewWsManager` following established pattern
     - Handle thread-specific subscriptions
     - Implement real-time message updates

2. **Cleanup** [Updated]:
   - Begin removing old WebSocket code from Dashboard component
   - Identify shared code that can be moved to common utilities
   - Plan Chat View migration

## 4. Testing Requirements

1. **Unit Tests**:
   - Message serialization/deserialization
   - Topic subscription logic
   - Reconnection behavior
   - State management
   - Component manager testing using trait-based mocks (`ITopicManager`)
   - Handler lifecycle and cleanup verification

2. **Integration Tests**:
   - Connection lifecycle
   - Message flow
   - Topic subscription/unsubscription
   - View-specific behaviors
   - Add tests for view-specific event handling and state updates.

3. **End-to-End Tests**:
   - Full user workflows
   - Error scenarios
   - Performance metrics

## 5. Risk Assessment

1. **Technical Risks**:
   - Message ordering during reconnection
   - Memory leaks in long-running connections
   - Browser compatibility issues
   - Performance impact of single connection

2. **Migration Risks**:
   - Data loss during transition
   - Backward compatibility issues
   - User experience disruption

## 6. Success Metrics

1. **Connection Metrics**:
   - Number of active connections
   - Connection lifetime
   - Reconnection frequency
   - Message latency

2. **Code Quality**:
   - Test coverage
   - Error handling completeness
   - Documentation quality

## 7. Next Actions [Updated Timeline]

1. **Immediate (Next 1-2 Days)**:
   - ✓ Completed initial Dashboard WebSocket integration
   - Test Dashboard real-time updates in various scenarios
   - Begin Chat View migration planning

2. **Short Term (1 Week)**:
   - Complete Dashboard testing and refinements
   - Implement Chat View WebSocket integration
   - Add comprehensive tests for both components

3. **Medium Term (2-3 Weeks)**:
   - Complete all view migrations
   - Remove old WebSocket code
   - Performance optimization
   - Documentation updates

## 8. Open Questions [Partially Addressed]

1. **Architecture**:
   - How to handle view-specific message processing? -> *Addressed: Via `TopicManager` handlers within components dispatching `AppState` messages.*
   - Best approach for global state updates? -> *Addressed: Confirmed `Rc<RefCell<AppState>>` with `dispatch_global_message` pattern.*
   - Strategy for handling offline mode? -> *Remains Open: Current focus is online mode.*
   - Testing global state interactions? -> *Partially Addressed: Using traits for mocking, but singleton initialization testing needs further exploration.*

2. **Performance**:
   - Impact of single connection on memory usage?
   - Message queuing strategy during disconnection?
   - Optimal ping interval for different network conditions?

3. **Testing**:
   - How to properly test WebSocket in Rust/WASM environment?
   - Best approach for mocking WebSocket in tests?
   - Performance testing methodology?

## 9. Dependencies

1. **Required Crate Updates**:
   - serde for serialization
   - uuid for message IDs
   - wasm-bindgen-test for testing
   - web-sys for browser APIs

2. **Browser Requirements**:
   - WebSocket support
   - JSON parsing capabilities
   - Local storage for state persistence

The focus now shifts to **integrating views** with the new topic-based WebSocket system, starting with the Dashboard.
