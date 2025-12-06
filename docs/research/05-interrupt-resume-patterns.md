# Interrupt and Resume Patterns for Long-Running Agents

## Summary

LangGraph provides built-in support for pausing agent execution via `interrupt()` and resuming from durable checkpoints. The key mechanism is checkpointing state to PostgreSQL (via `PostgresSaver`) instead of in-memory (`MemorySaver`), enabling agents to pause for external events (emails, time delays, human approval) and resume hours or days later. Zerg's existing trigger system (email, webhook, schedule) can be extended to serve as "wake conditions" that resume paused agent threads by invoking the graph with the saved checkpoint.

## LangGraph Interrupt Mechanism

### How Interrupts Work

LangGraph's `interrupt()` function allows an agent to pause mid-execution and save its state:

```python
from langgraph.types import interrupt

def approval_node(state):
    # Pause and ask for approval
    approved = interrupt("Do you approve this action?")
    return {"approved": approved}
```

When `interrupt()` is called:

1. The current graph state is serialized to the checkpointer
2. Execution halts and returns an `Interrupt` object
3. The graph can be resumed later by invoking with the same thread/checkpoint ID

### Key Concepts

- **Checkpointer**: Stores graph state between invocations
- **Thread ID**: Unique identifier for a conversation/execution thread
- **Checkpoint ID**: Specific point in time within a thread
- **Resume**: Call `graph.invoke()` with the same thread_id to continue from the last checkpoint

### State Capture

When paused, LangGraph captures:

- All state variables in the graph's state schema
- Current node position in the execution flow
- Message history
- Tool call results
- Custom metadata

## Durable Checkpointing Options

### MemorySaver (Current Zerg Implementation)

**Location**: `zerg/agents_def/zerg_react_agent.py:133`

```python
from langgraph.checkpoint.memory import MemorySaver
checkpointer = MemorySaver()
```

**Characteristics**:

- Stores state in process memory
- Fast access during runtime
- Lost on application restart or crash
- Suitable for: Short-lived conversations, single-session interactions

**Current Usage in Zerg**:

- Agent execution state is checkpointed in memory
- Thread messages are persisted to PostgreSQL separately (via `thread_service.py`)
- State is lost if backend restarts mid-execution

### PostgresSaver (Recommended for Interrupts)

**Installation**:

```bash
pip install langgraph-checkpoint-postgres
```

**Setup**:

```python
from langgraph.checkpoint.postgres import PostgresSaver

# Using Zerg's existing database connection
connection_string = "postgresql://user:pass@host:5432/zerg_db"
checkpointer = PostgresSaver(connection_string=connection_string)

# Or reuse existing SQLAlchemy engine
from zerg.database import engine
checkpointer = PostgresSaver.from_conn_string(str(engine.url))
```

**Characteristics**:

- Persists state to PostgreSQL tables
- Survives application restarts
- Enables resumption after hours/days
- Supports concurrent access with proper locking
- Suitable for: Long-running workflows, async triggers, human-in-the-loop

**Schema**:
PostgresSaver creates tables for:

- `checkpoints`: Full state snapshots
- `checkpoint_writes`: Incremental state updates
- Automatic cleanup of old checkpoints

## Types of Wake Conditions

### 1. Time-Based (Sleep/Schedule)

**Use Case**: "Wait 2 hours then check status"

**Pattern**:

```python
def wait_node(state):
    # Store wake time in state
    wake_time = datetime.now() + timedelta(hours=2)
    interrupt({"wake_time": wake_time.isoformat()})
    return state

# Resume via scheduler
# When scheduler sees wake_time has passed, call:
graph.invoke(input=None, config={"configurable": {"thread_id": thread_id}})
```

**Integration with Zerg**:

- Extend `scheduler_service.py` to schedule "resume" jobs
- Store wake_time in `Thread.agent_state` JSON field
- Scheduler polls for threads with `agent_state.wake_time < now()`

### 2. Event-Based (Email/Webhook)

**Use Case**: "Wait for email from customer then continue"

**Pattern**:

```python
def wait_for_email(state):
    # Create email trigger condition
    trigger_config = {
        "type": "email",
        "from_pattern": "customer@example.com",
        "subject_pattern": "Re: Quote Request"
    }
    interrupt({"waiting_for": "email", "trigger": trigger_config})
    return state

# Resume when email arrives
# email_trigger_service.py detects match and calls:
graph.invoke(
    input={"email_content": email_body},
    config={"configurable": {"thread_id": thread_id}}
)
```

**Integration with Zerg**:

- Link `Trigger` table to specific `Thread` (add `thread_id` FK)
- When trigger fires, check if thread is in "interrupted" state
- Resume graph with trigger payload as input

### 3. Approval-Based (Human-in-the-Loop)

**Use Case**: "Review agent's plan before executing expensive API call"

**Pattern**:

```python
def approval_gate(state):
    plan = state["proposed_action"]
    approved = interrupt({
        "type": "approval_required",
        "plan": plan,
        "ui_url": f"/approve/{thread_id}"
    })
    return {"approved": approved}

# Resume when user approves via UI
# Frontend posts to /threads/{thread_id}/resume with:
graph.invoke(
    input={"approved": True, "user_notes": "Looks good"},
    config={"configurable": {"thread_id": thread_id}}
)
```

**Integration with Zerg**:

- Add `interrupted_at` timestamp to `Thread` model
- New API endpoint: `POST /threads/{id}/resume` with resume payload
- UI shows paused threads with "Resume" button

## Current Zerg Trigger System

### Existing Components

**1. Trigger Model** (`models/models.py:302-357`)

- Types: `webhook`, `email` (provider: gmail)
- Config: JSON blob for provider-specific settings
- Linked to Agent, not Thread (one-to-many)

**2. Email Trigger Service** (`services/email_trigger_service.py`)

- Background polling loop (10-minute interval)
- Delegates to provider implementations (Gmail, SMTP)
- Handles watch renewal for Gmail push notifications

**3. Scheduler Service** (`services/scheduler_service.py`)

- APScheduler for cron-based agent execution
- Event bus integration for dynamic scheduling
- Creates new threads for each scheduled run

**4. Thread/Message Persistence** (`services/thread_service.py`)

- Converts between LangChain messages and DB rows
- Temporal awareness via timestamp prefixes
- Separate from LangGraph checkpointing

### Current Limitations for Interrupt/Resume

1. **No Thread-Level Triggers**: Triggers are agent-scoped, not thread-scoped
   - Can't say "resume THIS conversation when email arrives"
   - Each trigger fires starts a NEW thread

2. **No Checkpoint Durability**: MemorySaver loses state on restart
   - Can't resume after deployment or crash
   - Limited to in-memory execution lifetime

3. **No Interrupt State Tracking**: No field to mark thread as "waiting"
   - Can't distinguish paused vs completed threads
   - No metadata about what thread is waiting for

4. **Message History != Execution State**:
   - Messages are durable (PostgreSQL)
   - But agent's internal state (variables, node position) is not

## Proposed Integration

### Phase 1: Durable Checkpointing

**Goal**: Switch from MemorySaver to PostgresSaver

**Changes**:

```python
# zerg/agents_def/zerg_react_agent.py
from langgraph.checkpoint.postgres import PostgresSaver
from zerg.database import engine

def get_runnable(agent_row):
    # Replace MemorySaver with PostgresSaver
    checkpointer = PostgresSaver.from_conn_string(str(engine.url))

    @entrypoint(checkpointer=checkpointer)
    def agent_executor(messages, *, previous=None):
        # ... existing logic
```

**Database Migration**:

```sql
-- PostgresSaver creates these automatically on first use
-- checkpoints table
-- checkpoint_writes table
-- Optional: Add index on thread_id for faster lookups
```

**Benefits**:

- Agents can survive restarts
- Foundation for async resume
- No API changes required

### Phase 2: Thread-Level Wake Conditions

**Goal**: Support interrupt/resume with typed wake conditions

**Schema Changes**:

```python
# Add to Thread model (models/models.py)
class Thread(Base):
    # ... existing fields

    # New fields for interrupt/resume
    is_interrupted = Column(Boolean, default=False)
    interrupted_at = Column(DateTime, nullable=True)
    wake_condition = Column(JSON, nullable=True)  # {"type": "email|time|approval", ...}
    checkpoint_id = Column(String, nullable=True)  # LangGraph checkpoint ID
```

**Wake Condition Schema**:

```python
# New model in models/trigger_config.py or similar
class WakeCondition(BaseModel):
    type: Literal["time", "email", "webhook", "approval"]
    # Time-based
    wake_time: Optional[datetime]
    # Email-based
    email_pattern: Optional[Dict[str, str]]  # {"from": "...", "subject": "..."}
    # Webhook-based
    webhook_path: Optional[str]
    # Approval-based
    approval_ui: Optional[str]
```

### Phase 3: Resume Orchestration

**Goal**: Trigger services can resume paused threads

**New Service**: `services/agent_resume_service.py`

```python
class AgentResumeService:
    """Resumes interrupted agent threads when wake conditions are met."""

    async def check_time_based_resumes(self):
        """Find threads with wake_time <= now() and resume them."""
        with db_session() as db:
            threads = db.query(Thread).filter(
                Thread.is_interrupted == True,
                Thread.wake_condition["type"].astext == "time",
                Thread.wake_condition["wake_time"].astext <= datetime.now().isoformat()
            ).all()

            for thread in threads:
                await self.resume_thread(thread.id, input_data=None)

    async def resume_thread(self, thread_id: int, input_data: dict = None):
        """Resume a paused thread by invoking LangGraph with checkpoint."""
        with db_session() as db:
            thread = crud.get_thread(db, thread_id)
            agent = crud.get_agent(db, thread.agent_id)

            # Get the compiled graph
            graph = get_runnable(agent)

            # Resume from checkpoint
            config = {
                "configurable": {
                    "thread_id": str(thread_id),
                    "checkpoint_id": thread.checkpoint_id
                }
            }

            result = await graph.ainvoke(input_data or {}, config=config)

            # Update thread state
            thread.is_interrupted = False
            thread.interrupted_at = None
            thread.wake_condition = None
            db.commit()

            return result
```

**Integration Points**:

1. **Email Trigger Service**:

```python
# In email_trigger_service.py
async def _check_email_triggers(self):
    # ... existing logic to fetch emails

    # NEW: Check for threads waiting on this email
    with db_session() as db:
        waiting_threads = db.query(Thread).filter(
            Thread.is_interrupted == True,
            Thread.wake_condition["type"].astext == "email",
            # Match email pattern from wake_condition
        ).all()

        for thread in waiting_threads:
            if email_matches_pattern(email, thread.wake_condition):
                await resume_service.resume_thread(
                    thread.id,
                    input_data={"email": serialize_email(email)}
                )
```

2. **Scheduler Service**:

```python
# In scheduler_service.py
async def start(self):
    # ... existing scheduler setup

    # NEW: Add job to check for time-based resumes
    self.scheduler.add_job(
        resume_service.check_time_based_resumes,
        IntervalTrigger(minutes=1),
        id="check_resumes"
    )
```

3. **New API Endpoint**:

```python
# In routers/threads.py
@router.post("/{thread_id}/resume")
async def resume_thread(
    thread_id: int,
    resume_data: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Resume a paused thread (for approval-based resumes)."""
    thread = crud.get_thread(db, thread_id)

    # Verify thread belongs to user's agent
    if thread.agent.owner_id != current_user.id:
        raise HTTPException(403)

    if not thread.is_interrupted:
        raise HTTPException(400, "Thread is not paused")

    result = await resume_service.resume_thread(thread_id, resume_data)
    return {"status": "resumed", "result": result}
```

## Implementation Status

### Phase 1: Durable Checkpointing - COMPLETED (2025-12-03)

**Implementation Details**:

- Added `langgraph-checkpoint-postgres>=2.0.0` to dependencies (pyproject.toml)
- Created checkpointer factory service (`zerg/services/checkpointer.py`)
- Modified agent definition to use factory instead of hardcoded MemorySaver
- Comprehensive test coverage (10 tests, all passing)

**Key Files Modified**:

- `/Users/davidrose/git/zerg/apps/zerg/backend/pyproject.toml` - Added dependency
- `/Users/davidrose/git/zerg/apps/zerg/backend/zerg/services/checkpointer.py` - New factory module
- `/Users/davidrose/git/zerg/apps/zerg/backend/zerg/agents_def/zerg_react_agent.py` - Use factory
- `/Users/davidrose/git/zerg/apps/zerg/backend/tests/test_checkpointer.py` - New tests
- `/Users/davidrose/git/zerg/apps/zerg/backend/tests/test_zerg_react_agent.py` - Updated for compatibility

**Architecture Decision**:

- **Factory Pattern**: `get_checkpointer(engine)` inspects database URL
- **PostgreSQL**: Returns `PostgresSaver` with automatic table creation
- **SQLite**: Returns `MemorySaver` for fast tests
- **Fallback**: Returns `MemorySaver` on setup failure or unknown DB type
- **Caching**: PostgresSaver instances cached by connection URL

**Database Schema**:
PostgresSaver automatically creates:

- `checkpoints` table - Full state snapshots
- `checkpoint_writes` table - Incremental updates

**Benefits Achieved**:

- Agent checkpoints survive process restarts
- Foundation for interrupt/resume patterns
- No API changes required
- Backward compatible with existing code

**Test Results**:

- New checkpointer tests: 10/10 passing
- Existing agent tests: 2/2 passing (updated for thread_id config)

**Next Steps** (Not Implemented):
See remaining implementation phases below.

## Implementation Steps

### Step 1: Add PostgresSaver (Low Risk) - ✅ COMPLETED

- ✅ Install `langgraph-checkpoint-postgres`
- ✅ Replace MemorySaver in `zerg_react_agent.py` with factory
- ✅ Test that existing functionality still works
- ✅ Verify checkpoints are being written to PostgreSQL

### Step 2: Database Schema Updates

- Add migration for Thread fields: `is_interrupted`, `wake_condition`, etc.
- Test schema changes in dev environment
- Backfill existing threads with default values

### Step 3: Implement Agent Interrupt Primitives

- Add helper functions for agents to call `interrupt()`
- Create typed wake condition models
- Test simple interrupt/resume flows manually

### Step 4: Build Resume Service

- Implement `AgentResumeService` with resume orchestration
- Add time-based resume checking
- Wire into scheduler service

### Step 5: Integrate with Trigger System

- Extend email trigger service to detect resume conditions
- Add webhook resume endpoint
- Test end-to-end email → resume flow

### Step 6: UI Support

- Show interrupted threads in thread list
- Add "Resume" button with approval UI
- Display wake condition metadata

### Step 7: Documentation & Examples

- Write docs for agent developers on using interrupts
- Create example agents: "Wait for Email", "Request Approval"
- Add integration tests for common patterns

## Example: "Wait for Email Then Continue" Flow

### Agent Implementation

```python
# In an agent's task instructions or as a tool
def wait_for_customer_reply(state):
    """Pause until customer responds to our email."""

    # Agent has sent an email, now wait for reply
    wake_condition = {
        "type": "email",
        "provider": "gmail",
        "from_pattern": state["customer_email"],
        "subject_pattern": f"Re: {state['email_subject']}"
    }

    # This pauses execution and saves state to PostgreSQL
    interrupt({
        "waiting_for": "customer_reply",
        "wake_condition": wake_condition,
        "timeout_hours": 48  # Optional: auto-fail after 2 days
    })

    # When resumed, state will include the email content
    return state
```

### System Flow

1. **Agent Execution**:

   ```
   User: "Email customer for approval then proceed"
   Agent: [sends email via tool]
   Agent: [calls wait_for_customer_reply()]
   → interrupt() is called
   → State saved to PostgreSQL checkpoint
   → Thread.is_interrupted = True
   → Thread.wake_condition = {...}
   → API returns to user: "Waiting for customer reply"
   ```

2. **Background Polling** (email_trigger_service):

   ```
   Every 10 minutes:
   → Check Gmail for new messages
   → Match against wake_condition patterns
   → If match found:
       → Load thread and checkpoint
       → Call resume_service.resume_thread(thread_id, email_data)
   ```

3. **Resume Execution**:

   ```
   resume_service.resume_thread():
   → Load agent graph with PostgresSaver
   → Invoke with thread_id and checkpoint_id
   → Graph resumes from interrupt() point
   → state now includes email content
   → Agent continues: "Customer approved, proceeding with order"
   → Thread.is_interrupted = False
   ```

4. **UI Updates**:
   ```
   Thread list shows:
   [⏸] "Email Campaign Follow-up"
       Waiting for: customer@example.com reply
       Paused: 2 hours ago
       [Resume] [Cancel]
   ```

### Error Handling

**Timeout**:

```python
# In resume service
async def check_timeouts(self):
    with db_session() as db:
        expired = db.query(Thread).filter(
            Thread.is_interrupted == True,
            Thread.interrupted_at < datetime.now() - timedelta(hours=48)
        ).all()

        for thread in expired:
            # Resume with timeout signal
            await self.resume_thread(
                thread.id,
                input_data={"timeout": True, "reason": "No reply after 48 hours"}
            )
```

**Invalid Resume Attempt**:

```python
# In resume endpoint
if not thread.is_interrupted:
    return {"error": "Thread is not paused", "status": "already_completed"}
```

## Comparison: MemGPT vs LangGraph Approaches

**MemGPT**:

- Treats memory as first-class state
- Interrupts are implicit (context window overflow)
- Pause/resume via memory serialization
- Focus: Long-term conversational memory

**LangGraph**:

- Explicit `interrupt()` calls in graph nodes
- Checkpointing infrastructure for state persistence
- Designed for workflow orchestration
- Focus: Multi-step agent workflows with external dependencies

**Zerg's Approach** (Recommended):

- Use LangGraph's native interrupt/checkpoint system
- Extend with typed wake conditions for different trigger types
- Leverage existing trigger infrastructure (email, webhook, schedule)
- Persist both messages (current) AND execution state (new)

---

**References**:

- LangGraph Interrupts: https://docs.langchain.com/langgraph/interrupts
- PostgresSaver: https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-postgres
- Zerg codebase: `/Users/davidrose/git/zerg/apps/zerg/backend/`
