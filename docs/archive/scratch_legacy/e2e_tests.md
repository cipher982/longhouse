# E2E Test Expansion Plan for Zerg Agent Platform

## Context

The Zerg Agent Platform is a sophisticated AI agent management system with:
- **Frontend**: Rust/WASM-based SPA with multiple views (Dashboard, Canvas, Profile, Chat)
- **Backend**: FastAPI with PostgreSQL, supporting agents, threads, triggers, and real-time WebSocket communication
- **Current E2E Coverage**: Only 4 basic tests covering dashboard rendering, scope toggle, canvas agent shelf, and modal tabs

## Goals

1. **Comprehensive Coverage**: Test all major user workflows end-to-end
2. **User-Centric Testing**: Focus on typical user journeys rather than implementation details
3. **Regression Prevention**: Catch integration issues between frontend and backend
4. **Documentation**: Tests serve as living documentation of system behavior
5. **Confidence**: Enable rapid development with safety net of thorough tests

## Test Implementation Checklist

### 🔴 Priority 1: Core Agent Management
- [x] **agent_crud.spec.ts**
  - [x] Create agent with minimal configuration
  - [x] Create agent with all fields (instructions, model, temperature)
  - [x] Validate required fields show errors
  - [x] Edit agent name and verify update
  - [x] Edit agent instructions
  - [x] Change agent model selection
  - [x] Delete agent with confirmation dialog
  - [x] Cancel delete operation
  - [x] Verify agent appears in dashboard after creation
  - [x] Test agent status toggle (active/inactive)

### 🔴 Priority 2: Thread & Chat Functionality
- [x] **thread_chat.spec.ts**
  - [x] Create new thread from agent dashboard
  - [x] Send user message in thread
  - [x] Wait for and verify agent response
  - [x] Send follow-up message in same thread
  - [x] Create multiple threads for same agent
  - [x] Switch between threads
  - [x] Delete thread and verify removal
  - [x] Test thread title editing
  - [x] Verify message history persistence
  - [x] Test empty thread state

### 🔴 Priority 3: Authentication & Authorization
- [x] **auth_flows.spec.ts**
  - [ ] Test login redirect when unauthenticated
  - [ ] Mock Google OAuth flow
  - [ ] Verify user profile data after login
  - [ ] Test logout functionality
  - [ ] Verify session persistence on reload
  - [ ] Test unauthorized access attempts
  - [ ] Admin vs regular user permissions
  - [ ] Profile view navigation and display
  - [x] Test login redirect when unauthenticated
  - [x] Mock Google OAuth flow
  - [x] Verify user profile data after login
  - [x] Test logout functionality
  - [x] Verify session persistence on reload
  - [x] Test unauthorized access attempts
  - [x] Admin vs regular user permissions
  - [x] Profile view navigation and display

### 🟡 Priority 4: Canvas Editor
- [x] **canvas_workflows.spec.ts**
  - [x] Drag agent from shelf to canvas
  - [x] Position node at specific coordinates
  - [x] Create edge between two nodes
  - [x] Delete node from canvas
  - [ ] Delete edge between nodes
  - [ ] Select multiple nodes
  - [ ] Save workflow state
  - [ ] Load saved workflow
  - [ ] Clear canvas
  - [ ] Test zoom/pan controls
  - [x] Delete edge between nodes
  - [x] Select multiple nodes
  - [x] Save workflow state
  - [x] Load saved workflow
  - [x] Clear canvas
  - [x] Test zoom/pan controls

### 🟡 Priority 5: Agent Scheduling
- [x] **agent_scheduling.spec.ts**
  - [x] Set cron schedule on agent
  - [x] Verify next_run_at displays correctly
  - [x] Edit existing schedule
  - [x] Remove schedule from agent
  - [x] Test invalid cron expressions
  - [x] Verify scheduled status indicator
  - [x] Test schedule in different timezones
  - [x] View last_run_at after execution

### 🟡 Priority 6: Trigger Management
- [x] **trigger_webhooks.spec.ts**
  - [x] Create webhook trigger
  - [ ] Copy webhook URL
  - [ ] View webhook secret
  - [x] Delete webhook trigger
  - [ ] Test multiple triggers per agent
  - [x] Copy webhook URL
  - [x] View webhook secret
  - [x] Test multiple triggers per agent

- [x] **trigger_email.spec.ts**
  - [ ] Create Gmail trigger
  - [ ] Configure email filters
  - [ ] Test Gmail connection status
  - [ ] Remove email trigger
  - [ ] Handle disconnected Gmail account
  - [x] Create Gmail trigger
  - [x] Configure email filters
  - [x] Test Gmail connection status
  - [x] Remove email trigger
  - [x] Handle disconnected Gmail account

### 🟢 Priority 7: Run History & Monitoring
- [x] **agent_runs.spec.ts**
  - [ ] View run history for agent
  - [ ] Check run status indicators
  - [ ] View run details (duration, tokens)
  - [ ] Filter runs by status
  - [ ] View error details for failed runs
  - [ ] Test pagination of run history
  - [ ] Export run data
  - [x] View run history for agent
  - [x] Check run status indicators
  - [x] View run details (duration, tokens)
  - [x] Filter runs by status
  - [x] View error details for failed runs
  - [x] Test pagination of run history
  - [x] Export run data

### 🟢 Priority 8: Real-time Features
- [x] **realtime_updates.spec.ts**
  - [ ] Test live dashboard updates
  - [ ] Verify WebSocket connection
  - [ ] Test message streaming
  - [ ] Multi-tab synchronization
  - [ ] Connection recovery after disconnect
  - [ ] Test presence indicators
  - [x] Test live dashboard updates
  - [x] Verify WebSocket connection
  - [x] Test message streaming
  - [x] Multi-tab synchronization
  - [x] Connection recovery after disconnect
  - [x] Test presence indicators

### 🟢 Priority 9: Search & Filtering
- [x] **search_filter.spec.ts**
  - [ ] Search agents by name
  - [ ] Filter by agent status
  - [ ] Filter by owner (admin view)
  - [ ] Sort by name ascending/descending
  - [ ] Sort by created date
  - [ ] Sort by last run time
  - [ ] Combine search and filters
  - [ ] Clear all filters
  - [x] Search agents by name
  - [x] Filter by agent status
  - [x] Filter by owner (admin view)
  - [x] Sort by name ascending/descending
  - [x] Sort by created date
  - [x] Sort by last run time
  - [x] Combine search and filters
  - [x] Clear all filters

### 🔵 Priority 10: Error Handling
- [x] **error_scenarios.spec.ts**
  - [ ] Network timeout handling
  - [ ] 404 error pages
  - [ ] Form validation errors
  - [ ] API error messages
  - [ ] Session expiry handling
  - [ ] Rate limit responses
  - [ ] Offline mode behavior
  - [x] Network timeout handling
  - [x] 404 error pages
  - [x] Form validation errors
  - [x] API error messages
  - [x] Session expiry handling
  - [x] Rate limit responses
  - [x] Offline mode behavior

### 🔵 Priority 11: Performance Tests
- [x] **performance.spec.ts**
  - [ ] Load dashboard with 100+ agents
  - [ ] Scroll through long message history
  - [ ] Rapid message sending
  - [ ] Multiple concurrent operations
  - [ ] Large canvas with many nodes
  - [ ] Memory leak detection
  - [x] Load dashboard with 100+ agents
  - [x] Scroll through long message history
  - [x] Rapid message sending
  - [x] Multiple concurrent operations
  - [x] Large canvas with many nodes
  - [x] Memory leak detection

## Implementation Guidelines

### Test Structure
```typescript
// Example test structure
test.describe('Feature: Agent Management', () => {
  test.beforeEach(async ({ page }) => {
    // Setup: Login, navigate to dashboard
  });

  test('should create new agent successfully', async ({ page }) => {
    // Arrange
    // Act
    // Assert
  });
});
```

### Best Practices
- ✅ Use data-testid attributes for reliable element selection
- ✅ Test user-visible behavior, not implementation
- ✅ Include both happy path and error scenarios
- ✅ Use meaningful test descriptions
- ✅ Keep tests independent and idempotent
- ✅ Mock external services (OAuth, email) where needed
- ✅ Use Page Object Model for complex interactions
