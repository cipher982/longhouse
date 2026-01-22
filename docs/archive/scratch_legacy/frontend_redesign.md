# AI Agent Platform Frontend Refactor Roadmap

## Current Issues
- View management is fragmented and unreliable
- Global state access patterns lead to bugs
- Component lifecycle not properly managed
- Unclear boundaries between views and components
- DOM manipulation scattered throughout codebase

## Target Architecture

### 1. Core Structure
```
src/
├─ views/              # View components and management
│  ├─ mod.rs          # View traits and manager
│  ├─ dashboard.rs    # Dashboard view
│  ├─ chat.rs         # Chat interface
│  └─ canvas.rs       # Canvas visualization
│
├─ components/         # Reusable UI components
│  ├─ mod.rs
│  ├─ thread_list.rs
│  ├─ agent_card.rs
│  └─ message_view.rs
│
├─ state/             # State management
│  ├─ mod.rs          # State container and access patterns
│  ├─ app.rs          # Core application state
│  └─ view.rs         # View-specific state types
│
├─ messages/          # Message handling
│  ├─ mod.rs          # Message types
│  └─ handlers.rs     # Message handling logic
│
└─ api/               # API communication
   ├─ mod.rs          # API client trait
   ├─ websocket.rs    # WebSocket handling
   └─ http.rs         # HTTP client implementation
```

### 2. Core Traits and Types

```rust
// View Management
trait View {
    fn mount(&self, document: &Document) -> Result<(), JsValue>;
    fn unmount(&self, document: &Document) -> Result<(), JsValue>;
    fn update(&self, state: &AppState) -> Result<(), JsValue>;
}

// Component Base
trait Component {
    type Props;

    fn render(&self, props: &Self::Props) -> Result<Element, JsValue>;
    fn update(&mut self, props: &Self::Props) -> Result<(), JsValue>;
}

// State Management
struct AppState {
    current_view: ViewType,
    agents: HashMap<AgentId, Agent>,
    threads: HashMap<ThreadId, Thread>,
    ui_state: UiState,
}

// Message System
enum Message {
    View(ViewMessage),
    Agent(AgentMessage),
    Thread(ThreadMessage),
    UI(UiMessage),
}
```

### 3. Implementation Phases

#### Phase 1: View Management
1. Implement `ViewManager` for proper view lifecycle
2. Create base `View` implementations for Dashboard, Chat, Canvas
3. Ensure clean mount/unmount cycles
4. Implement view-specific state management

```rust
struct ViewManager {
    current_view: Option<Box<dyn View>>,
    document: Document,
}

impl ViewManager {
    pub fn switch_to(&mut self, new_view: Box<dyn View>) -> Result<(), JsValue> {
        if let Some(current) = &self.current_view {
            current.unmount(&self.document)?;
        }
        new_view.mount(&self.document)?;
        self.current_view = Some(new_view);
        Ok(())
    }
}
```

#### Phase 2: Component System
1. Create reusable UI components
2. Implement proper component lifecycle
3. Define clear component interfaces
4. Create component test utilities

```rust
struct ThreadList {
    container: Element,
    threads: Vec<Thread>,
}

impl Component for ThreadList {
    type Props = ThreadListProps;

    fn render(&self, props: &Self::Props) -> Result<Element, JsValue> {
        // Clean rendering logic
        let container = document.create_element("div")?;
        // ... render threads
        Ok(container)
    }
}
```

#### Phase 3: State Management
1. Implement centralized state container
2. Create type-safe state access patterns
3. Add state change notifications
4. Implement state persistence

```rust
struct StateContainer {
    state: AppState,
    subscribers: Vec<Box<dyn Fn(&AppState)>>,
}

impl StateContainer {
    pub fn update<F>(&mut self, f: F)
    where
        F: FnOnce(&mut AppState)
    {
        f(&mut self.state);
        self.notify_subscribers();
    }
}
```

#### Phase 4: Message System
1. Define message types for all operations
2. Implement message handlers
3. Create message dispatch system
4. Add message logging/debugging

```rust
struct MessageDispatcher {
    state: StateContainer,
    handlers: HashMap<MessageType, Box<dyn MessageHandler>>,
}

impl MessageDispatcher {
    pub fn dispatch(&mut self, msg: Message) -> Result<(), JsValue> {
        let handler = self.handlers.get(&msg.type_id())?;
        handler.handle(msg, &mut self.state)
    }
}
```

### 4. API Integration

```rust
trait ApiClient {
    async fn fetch_agents(&self) -> Result<Vec<Agent>, ApiError>;
    async fn create_thread(&self, agent_id: AgentId) -> Result<Thread, ApiError>;
    async fn send_message(&self, thread_id: ThreadId, content: String) -> Result<Message, ApiError>;
}

struct WebSocketManager {
    connection: WebSocket,
    message_handler: Box<dyn Fn(WebSocketMessage)>,
}
```

### 5. Testing Strategy
1. Unit tests for components
2. Integration tests for views
3. State management tests
4. Message handling tests
5. Mock API client for testing

```rust
#[cfg(test)]
mod tests {
    #[test]
    fn test_view_lifecycle() {
        let mut manager = ViewManager::new();
        let view = DashboardView::new();
        assert!(manager.switch_to(Box::new(view)).is_ok());
        // ... test cleanup
    }
}
```

### 6. Development Workflow
1. Use `wasm-pack` for building
2. Implement hot reloading
3. Add development tools
4. Create component playground

### Migration Strategy
1. Implement new architecture alongside existing code
2. Gradually migrate views one at a time
3. Add tests during migration
4. Remove old code paths
5. Validate each migration step


────────────────────────────────────────────────────────────────────────
4) Potential Pitfalls or Caveats
────────────────────────────────────────────────────────────────────────

• Incremental Refactor Complexity:
  It’s easy to break minor features during a big code reorganization. A safer path is to do it incrementally—e.g., rewrite the Chat view using the new architecture, ensure that it’s stable, then move on to the Dashboard, and so on.

• Over-Abstraction:
  Sometimes strict layering with a “View” trait and “Component” trait can become cumbersome for very small features. If something is trivially just a few lines, you might not need to formalize it with a trait. Use your best judgment to avoid too many tiny modules or traits.

• Continued Global “AppState” Use:
  Even if you move to separate modules, ensure each module doesn’t just do “APP_STATE.with(...)” to read/modify global data arbitrarily. That reintroduces tight coupling. Instead, define clear function parameters or pass around the relevant state slices (like “&mut DashboardState”) to keep modules more testable.

• Resist Merging Too Many responsibilities in a Single Type:
  The new approach is best if "View" code mostly handles user interactions and DOM updates, letting the domain or “Message” handlers take care of actual data logic. Keep them separate, so your domain logic (like “agent runs a task” or “new chat message arrives”) can be tested without DOM involvement.

────────────────────────────────────────────────────────────────────────
5) Suggested Path Forward
────────────────────────────────────────────────────────────────────────

1. Start with Low-Risk Features:
   Pick a smaller “view” or “component” (like the “model_selector” or “thread_list”) and refactor it to fit the proposed redesign. Wrap it in your new “View” or “Component” trait, handle props, do clean “mount” & “update” cycles. Confirm you like how it feels.

2. Gradually Migrate Larger Pieces (Canvas or Dashboard):
   Something like the “dashboard.rs” is large but a good candidate for a real “View.” You can rename the current “dashboard.rs” to “dashboard_legacy.rs,” then create a new “dashboard” that follows your “View” trait, and begin rewriting piece by piece.

3. Consolidate the “update.rs” or Break It into Domain-Specific Handlers:
   Right now, “update.rs” is a monolith. Over time, you can create smaller modules: “agent_update.rs,” “thread_update.rs,” “canvas_update.rs,” or a set of “handler” modules. Then your main “update(msg)” function just routes the message to the appropriate handler.

4. Improve Testing/Mocking:
   With smaller modules and clearly separated “API Client” traits, you can set up mocks for your tests. This will make your code more robust and ensure new contributors can test each piece in isolation.

5. Maintain a Consistent Conventions Document:
   Because your code is now large, a set of conventions (folder layout, naming patterns, how a new “View” or “Component” is structured, etc.) goes a long way. The “frontend_redesign.md” can become that blueprint as you expand your refactoring to the rest of the project.
