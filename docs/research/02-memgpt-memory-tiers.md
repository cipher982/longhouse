# MemGPT Memory Tier Architecture for Zerg

**Research Date:** 2025-12-03
**Paper Reference:** MemGPT: Towards LLMs as Operating Systems (arXiv:2310.08560)

## Summary

MemGPT provides a three-tier memory architecture (main context, external context, archival storage) inspired by OS virtual memory management, enabling LLMs to handle unbounded conversation lengths. Zerg currently stores all messages in a flat ThreadMessage table with basic "buffer" memory strategy. By implementing MemGPT-style memory tools and a supervisor agent, Zerg could compress old context into summaries stored in Thread.agent_state, evict historical messages to archival storage (new table), and dynamically retrieve relevant context, enabling long-running agents without token limit constraints.

## MemGPT Memory Model Explained

### Three-Tier Architecture

MemGPT solves the fixed context window problem by organizing memory into three distinct tiers, analogous to CPU registers, RAM, and disk storage:

#### 1. Main Context (In-Context Memory)
- **Equivalent to:** CPU registers / L1 cache
- **Purpose:** The LLM's immediate working memory within its fixed context window
- **Content:** Current conversation state, recent messages, active task details
- **Size:** Limited by model's context window (4k-128k tokens depending on model)
- **Access Pattern:** Direct access by LLM on every turn
- **In MemGPT:** This is the actual prompt sent to the LLM, carefully curated to stay within limits

#### 2. External Context (Recall Storage - Active)
- **Equivalent to:** RAM / Main memory
- **Purpose:** Medium-term memory of important facts and conversation details
- **Content:** Summarized conversation history, user preferences, agent persona, key facts
- **Size:** Larger than main context but still constrained (typically stored as structured summaries)
- **Access Pattern:** Loaded into main context when relevant via semantic search or recency
- **In MemGPT:** Stored in `core_memory` which has sections for persona and human info

#### 3. Archival Storage (Long-Term Memory)
- **Equivalent to:** Disk storage / Database
- **Purpose:** Unbounded long-term storage of all historical interactions
- **Content:** Full message history, documents, past conversations, archived facts
- **Size:** Unlimited (constrained only by storage capacity)
- **Access Pattern:** Retrieved via search queries when needed (semantic similarity, keywords, timestamps)
- **In MemGPT:** Separate vector database or relational storage with search capabilities

### Memory Management Tools

MemGPT gives the agent explicit tools to manage its own memory (self-editing):

1. **`core_memory_append(name, content)`** - Add new information to core memory sections
2. **`core_memory_replace(name, old_content, new_content)`** - Update existing core memory
3. **`archival_memory_insert(content)`** - Store information in long-term archival
4. **`archival_memory_search(query, page=0)`** - Retrieve from archival via search
5. **`conversation_search(query, page=0)`** - Search past conversation history
6. **`conversation_search_date(start_date, end_date)`** - Time-based retrieval

The agent actively decides when to:
- Compress recent messages into summary form (external context)
- Archive old details that might be needed later
- Search archives when current context lacks necessary information
- Update core beliefs/preferences based on new information

### Data Movement Between Tiers

**Main → External (Summarization):**
- Triggered when main context approaches token limit
- Agent uses LLM to generate summary of recent conversation segment
- Summary stored in structured external context (core_memory)
- Original detailed messages moved to archival

**External → Main (Context Loading):**
- On every turn, external context loaded into prompt header
- Agent sees: system prompt + core_memory + recent messages
- Core memory stays stable across turns until explicitly updated

**Archival → Main (Retrieval):**
- Agent explicitly calls search tools when needed
- Semantic similarity search finds relevant past interactions
- Retrieved content temporarily added to main context for current turn
- Agent decides what to remember long-term vs. one-time retrieval

**External → Archival (Eviction):**
- When external context grows too large for efficient management
- Older summarized segments moved to archival
- Core facts remain in external context (persona, preferences)

### Interrupt Mechanism

MemGPT implements pause/resume for long-running operations:

**Yield/Interrupt Flow:**
1. Agent executing multi-step task
2. User sends new message (interrupt signal)
3. System captures current state snapshot:
   - Current position in task
   - Working memory state
   - Pending tool calls
4. Agent pauses execution, handles interrupt
5. After response, agent can resume or pivot based on new context

**State Maintenance:**
- State snapshot stored in persistent memory tier
- Includes: conversation context, core memory, task progress
- Resume restores full state and continues from checkpoint
- Enables human-in-the-loop workflows without losing context

## Current Zerg Memory Architecture

### Data Model

**Thread Table** (`agent_threads`):
- `id` - Primary key
- `agent_id` - Foreign key to agents
- `title` - Human-readable thread name
- `active` - Boolean flag
- `agent_state` - JSON field (currently used for arbitrary metadata)
- `memory_strategy` - String field (currently hardcoded to "buffer")
- `thread_type` - Enum: chat, scheduled, manual
- `created_at`, `updated_at` - Timestamps

**ThreadMessage Table** (`thread_messages`):
- `id` - Primary key
- `thread_id` - Foreign key to threads
- `role` - "system", "user", "assistant", "tool"
- `content` - Text content of message
- `tool_calls` - JSON array of tool invocations (for assistant messages)
- `tool_call_id` - For tool response messages
- `name` - Tool name for tool messages
- `sent_at` - Timestamp (UTC)
- `processed` - Boolean flag for agent processing status
- `message_metadata` - JSON field for extensions
- `parent_id` - Self-referential FK for tool message grouping

**AgentRun Table** (`agent_runs`):
- `id` - Primary key
- `agent_id`, `thread_id` - Foreign keys
- `status` - Enum: queued, running, success, failed
- `trigger` - Enum: manual, schedule, api
- `started_at`, `finished_at`, `duration_ms` - Timing
- `total_tokens`, `total_cost_usd` - Usage metrics
- `error`, `cancel_reason` - Failure info
- `summary` - Text summary of run (for Jarvis inbox)

### Current Context Building Process

**AgentRunner.run_thread()** flow:
1. Load all ThreadMessages for thread via `get_thread_messages_as_langchain()`
2. Prepend connector protocols to system message
3. Inject ephemeral connector status context (not persisted)
4. Pass full message history to LangGraph runnable
5. LangGraph/LangChain handles context window internally
6. New assistant/tool messages appended to ThreadMessage table
7. No summarization or eviction occurs

**Current Limitations:**
- **No memory management** - All messages kept indefinitely in flat structure
- **Token limit risk** - Long threads will eventually hit context window limits
- **No compression** - Full message content always loaded
- **No archival** - No way to search old conversations or retrieve selectively
- **Unused fields** - `memory_strategy` exists but isn't implemented
- **agent_state** - JSON field available but not used for memory purposes

### Temporal Awareness

Zerg already implements temporal awareness (connector-aware agents PRD P1.2):
- Messages timestamped with `sent_at` field
- Timestamps prepended to user/assistant messages: `[2025-12-03T14:23:00Z] message content`
- System/tool messages not timestamped (static or immediate)
- Enables agent to understand time-based context and urgency

## Mapping MemGPT → Zerg

### Direct Architectural Mappings

| MemGPT Concept | Zerg Equivalent | Status | Notes |
|----------------|-----------------|--------|-------|
| Main Context | Messages loaded in AgentRunner | ✅ Exists | Currently loads ALL messages |
| External Context (core_memory) | Thread.agent_state JSON field | 🟡 Unused | Field exists but empty |
| Archival Storage | N/A | ❌ Missing | Need new table or service |
| core_memory_append | New supervisor tool | ❌ Missing | Would write to agent_state |
| core_memory_replace | New supervisor tool | ❌ Missing | Would update agent_state |
| archival_memory_insert | New supervisor tool | ❌ Missing | Would write to archival table |
| archival_memory_search | New supervisor tool | ❌ Missing | Would query archival table |
| conversation_search | ThreadMessage query | 🟡 Partial | DB query exists, no tool |
| Interrupt mechanism | AgentRun + Run status | 🟡 Partial | Basic pause exists via status |
| State snapshots | Thread.agent_state | 🟡 Unused | Field available |

### Implementation Strategy

**Phase 1: Core Memory (External Context)**
Use `Thread.agent_state` JSON field to store structured core memory:

```json
{
  "core_memory": {
    "persona": "I am a helpful agent that...",
    "human": "User prefers concise responses, works in PST timezone",
    "context": "Currently helping with project X, last discussed Y on 2025-12-01"
  },
  "conversation_summary": [
    {
      "date_range": "2025-11-01 to 2025-11-15",
      "turn_range": [0, 100],
      "summary": "User requested feature X, discussed implementation...",
      "key_facts": ["Prefers Python", "Deadline Dec 1", "Budget $500"]
    }
  ],
  "last_summarized_turn": 100
}
```

**Phase 2: Archival Storage**
Create new table for long-term message archival:

```sql
CREATE TABLE archived_messages (
    id INTEGER PRIMARY KEY,
    thread_id INTEGER REFERENCES agent_threads(id),
    original_message_id INTEGER,  -- Reference to original ThreadMessage
    role TEXT,
    content TEXT,
    sent_at TIMESTAMP,
    archived_at TIMESTAMP,
    embedding VECTOR(1536),  -- For semantic search (optional)
    metadata JSON
);
```

**Phase 3: Smart Context Loading**
Modify `AgentRunner.run_thread()` to:
1. Load core_memory from thread.agent_state
2. Load only recent N messages (e.g., last 50 turns or 8k tokens)
3. Check if summarization threshold reached
4. Inject core_memory into system prompt
5. Provide search tools if agent needs older context

**Phase 4: Supervisor Memory Tools**
Implement tools as callables available to agent:
- `update_core_memory(section, content)` - Updates thread.agent_state
- `summarize_recent_conversation(turn_count)` - Generates summary, updates agent_state
- `archive_old_messages(before_turn)` - Moves messages to archived_messages table
- `search_conversation_history(query, limit=5)` - Semantic search in archived messages
- `search_by_date(start, end)` - Time-based archival retrieval

### Key Differences from MemGPT

**What to Keep:**
- Three-tier memory model concept
- Self-editing via explicit tools
- Summarization to compress context
- Search-based archival retrieval

**What to Adapt:**
- **Use Thread.agent_state** instead of separate core_memory DB
- **Leverage existing Run model** for interrupt/resume state
- **Keep ThreadMessage as source of truth** - archival is copy, not move
- **Supervisor agent pattern** - Memory management as agentic behavior, not framework code
- **Temporal awareness already exists** - Timestamps in messages

**Zerg-Specific Advantages:**
- Already have Run model for execution tracking
- agent_state field purpose-built for state persistence
- Thread isolation - each agent/thread has independent memory
- Connector-aware architecture - memory can reference external systems
- Workflow engine integration - memory tools as nodes

## Proposed Memory Tools for Supervisor

### Tool Specifications

#### 1. Core Memory Management

```python
@tool
def update_core_memory(
    section: Literal["persona", "human", "context"],
    content: str,
    operation: Literal["append", "replace"] = "append"
) -> str:
    """
    Update the core memory (external context) for this conversation.

    Core memory persists across all turns and should contain essential
    facts, preferences, and context. Use this to remember important
    information long-term.

    Sections:
    - persona: Information about yourself, your capabilities, your role
    - human: Information about the user - preferences, background, goals
    - context: Current task/project context, recent decisions, next steps

    Args:
        section: Which core memory section to update
        content: The information to add or replace
        operation: "append" adds to existing content, "replace" overwrites

    Returns:
        Confirmation message with updated core memory state
    """
    # Implementation would:
    # 1. Load current thread.agent_state
    # 2. Update the specified section
    # 3. Save back to DB
    # 4. Return new state for agent awareness
```

#### 2. Conversation Summarization

```python
@tool
def summarize_conversation(
    start_turn: int,
    end_turn: int,
    focus: Optional[str] = None
) -> str:
    """
    Generate a summary of conversation turns and store in core memory.

    Use this when you notice the conversation history is getting long
    or when important decisions have been made that should be preserved
    in compressed form.

    Args:
        start_turn: Starting message index (0-based)
        end_turn: Ending message index (inclusive)
        focus: Optional focus area - "decisions", "facts", "context", "all"

    Returns:
        The generated summary (also saved to agent_state automatically)
    """
    # Implementation would:
    # 1. Fetch specified message range
    # 2. Use LLM to generate focused summary
    # 3. Append to conversation_summary in agent_state
    # 4. Update last_summarized_turn pointer
    # 5. Return summary for agent confirmation
```

#### 3. Archival Management

```python
@tool
def archive_old_messages(
    before_turn: int,
    preserve_summary: bool = True
) -> str:
    """
    Archive old messages to free up context window space.

    Archived messages are moved to long-term storage but remain
    searchable. Use this after summarizing a conversation segment
    to keep the active context window clean.

    Args:
        before_turn: Archive all messages before this turn index
        preserve_summary: If True, ensure a summary exists before archiving

    Returns:
        Confirmation with count of archived messages
    """
    # Implementation would:
    # 1. Check if summary exists for range (if preserve_summary=True)
    # 2. Copy messages to archived_messages table
    # 3. Mark original messages with archived flag (don't delete)
    # 4. Update context loading to skip archived messages
    # 5. Return stats
```

#### 4. Historical Search

```python
@tool
def search_conversation_history(
    query: str,
    search_archived: bool = True,
    limit: int = 5
) -> str:
    """
    Search past conversation for relevant information.

    Use this when you need to recall something from earlier in the
    conversation that's not in your current context window.

    Args:
        query: Natural language search query
        search_archived: Include archived messages (recommended)
        limit: Maximum number of results to return

    Returns:
        Formatted results with message content, timestamps, and context
    """
    # Implementation would:
    # 1. Perform semantic search on message content
    # 2. Include message embeddings (optional, fallback to text search)
    # 3. Search both active and archived messages
    # 4. Return ranked results with context
```

#### 5. Memory Inspection

```python
@tool
def view_core_memory() -> str:
    """
    View the current state of core memory (external context).

    Use this to review what you've stored in long-term memory
    to avoid redundant updates or to refresh your understanding
    of key facts.

    Returns:
        Formatted display of all core memory sections
    """
    # Implementation would:
    # 1. Load thread.agent_state
    # 2. Format core_memory sections nicely
    # 3. Include last_summarized_turn and summary count
    # 4. Return for agent review
```

### Usage Pattern Example

```
Agent Turn 150:
User: "Remember that I prefer Python over JavaScript"

Agent: [Thinks: This is a preference, should store in core memory]
<uses update_core_memory>
  section: "human"
  content: "Prefers Python over JavaScript for coding tasks"
  operation: "append"
</uses update_core_memory>

Result: "Updated core memory [human]. Current state: 'User is David, timezone PST. Prefers Python over JavaScript for coding tasks.'"

Agent: "Got it! I've noted your preference for Python. I'll keep that in mind for future recommendations."

---

Agent Turn 180:
[Context window at 75% capacity, 15k tokens used]

Agent: [Thinks: Conversation is getting long, should summarize turns 0-150]
<uses summarize_conversation>
  start_turn: 0
  end_turn: 150
  focus: "all"
</uses summarize_conversation>

Result: "Summary created for turns 0-150: User David discussed project setup, chose FastAPI framework, decided on PostgreSQL database, configured Docker. Key decisions: Python stack, REST API, deploy to Hetzner VPS. Stored in conversation_summary[0]."

Agent: [Now can archive old messages safely]
<uses archive_old_messages>
  before_turn: 150
  preserve_summary: true
</uses archive_old_messages>

Result: "Archived 150 messages to long-term storage. Summary preserved in core memory. Context window freed: ~10k tokens."
```

## Implementation Approach

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     Zerg Memory System                      │
└─────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────┐
  │  Tier 1: Main Context (AgentRunner)                      │
  │  • Recent messages (last 50 turns or 8k tokens)          │
  │  • Core memory injected from agent_state                 │
  │  • Ephemeral context (connector status, protocols)       │
  │  • Loaded fresh on every turn                            │
  └──────────────────────────────────────────────────────────┘
                              ↕
  ┌──────────────────────────────────────────────────────────┐
  │  Tier 2: External Context (Thread.agent_state)           │
  │  {                                                        │
  │    "core_memory": {                                       │
  │      "persona": "...",                                    │
  │      "human": "...",                                      │
  │      "context": "..."                                     │
  │    },                                                     │
  │    "conversation_summary": [...],                        │
  │    "last_summarized_turn": 150                           │
  │  }                                                        │
  │  • Persisted in Thread row (JSON)                        │
  │  • Updated via memory tools                              │
  │  • Loaded into prompt header                             │
  └──────────────────────────────────────────────────────────┘
                              ↕
  ┌──────────────────────────────────────────────────────────┐
  │  Tier 3: Archival Storage                                │
  │  • New archived_messages table                           │
  │  • Copy of old ThreadMessage rows                        │
  │  • Optional: embeddings for semantic search              │
  │  • Retrieved via search tools when needed                │
  │  • Unlimited capacity                                    │
  └──────────────────────────────────────────────────────────┘
```

### Phased Implementation Plan

#### Phase 1: Core Memory Foundation (2-3 days)
**Goal:** Enable basic core memory persistence without changing context loading

**Tasks:**
1. Define core_memory schema in `Thread.agent_state`
2. Implement `update_core_memory` tool
3. Implement `view_core_memory` tool
4. Add core_memory injection to `AgentRunner.run_thread()` system prompt
5. Test: Agent can store/retrieve preferences and facts

**Deliverables:**
- `zerg/tools/memory/core_memory_tools.py` - Tool implementations
- `zerg/services/memory_service.py` - Core memory CRUD helpers
- Updated `AgentRunner` to inject core_memory into prompt
- Unit tests for memory tools

**Success Criteria:**
- Agent can store facts in core_memory
- Core_memory persists across conversation turns
- Core_memory visible in prompt (via debug logging)

#### Phase 2: Conversation Summarization (3-4 days)
**Goal:** Enable automatic context compression

**Tasks:**
1. Implement `summarize_conversation` tool
2. Add conversation_summary schema to agent_state
3. Create summarization prompt template
4. Test: Long conversations get compressed into summaries

**Deliverables:**
- Summarization tool with LLM-based compression
- Conversation summary storage in agent_state
- Test fixtures with long conversation threads

**Success Criteria:**
- Agent can summarize past conversation segments
- Summaries stored in structured format
- Summaries include key facts and decisions

#### Phase 3: Smart Context Loading (2-3 days)
**Goal:** Load only recent messages + core memory instead of full history

**Tasks:**
1. Add `max_context_messages` config to Agent model
2. Modify `AgentRunner.run_thread()` to:
   - Load only recent N messages
   - Skip messages before last_summarized_turn
   - Inject core_memory in system prompt
3. Add context window monitoring and warnings
4. Test: Long threads stay under token limits

**Deliverables:**
- Updated context loading logic in AgentRunner
- Configuration for context window management
- Logging for context usage metrics

**Success Criteria:**
- Only recent messages loaded from DB
- Core memory injected correctly
- Token usage stays bounded for long threads

#### Phase 4: Archival Storage (4-5 days)
**Goal:** Store old messages in searchable archival tier

**Tasks:**
1. Create `archived_messages` migration
2. Implement `archive_old_messages` tool
3. Implement `search_conversation_history` tool
4. Add optional embeddings generation (pgvector)
5. Test: Archived messages searchable and retrievable

**Deliverables:**
- `archived_messages` table with indexes
- Archival tools (archive + search)
- Optional: Embedding generation pipeline
- Migration to handle existing long threads

**Success Criteria:**
- Old messages copied to archival table
- Search returns relevant results
- Archived messages excluded from default context loading

#### Phase 5: Supervisor Agent Integration (2-3 days)
**Goal:** Enable supervisor agent to use memory tools

**Tasks:**
1. Add memory tools to supervisor agent allowlist
2. Update supervisor system prompt to include memory protocol
3. Add memory management guidance to supervisor instructions
4. Test: Supervisor uses memory tools appropriately

**Deliverables:**
- Updated supervisor agent definition
- Memory management system prompt additions
- Documentation for supervisor memory usage

**Success Criteria:**
- Supervisor can manage memory for child agents
- Memory tools appear in supervisor's tool list
- Supervisor makes intelligent decisions about summarization

### Database Schema Changes

**New Table: archived_messages**
```sql
CREATE TABLE archived_messages (
    id SERIAL PRIMARY KEY,
    thread_id INTEGER NOT NULL REFERENCES agent_threads(id) ON DELETE CASCADE,
    original_message_id INTEGER REFERENCES thread_messages(id),
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    sent_at TIMESTAMP WITH TIME ZONE NOT NULL,
    archived_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    tool_calls JSON,
    tool_call_id VARCHAR(255),
    name VARCHAR(255),
    message_metadata JSON,
    embedding VECTOR(1536),  -- Optional, for pgvector semantic search

    INDEX idx_archived_messages_thread_id (thread_id),
    INDEX idx_archived_messages_sent_at (sent_at),
    INDEX idx_archived_messages_role (role)
);

-- Optional: GIN index for full-text search if not using embeddings
CREATE INDEX idx_archived_messages_content_gin ON archived_messages
    USING GIN(to_tsvector('english', content));
```

**Modified Column: thread_messages.is_archived**
```sql
ALTER TABLE thread_messages
ADD COLUMN is_archived BOOLEAN DEFAULT FALSE;

CREATE INDEX idx_thread_messages_is_archived
ON thread_messages(thread_id, is_archived);
```

**Agent State Schema (JSON in Thread.agent_state):**
```json
{
  "core_memory": {
    "persona": "string",
    "human": "string",
    "context": "string"
  },
  "conversation_summary": [
    {
      "date_range": "string",
      "turn_range": [int, int],
      "summary": "string",
      "key_facts": ["string"],
      "created_at": "ISO8601 timestamp"
    }
  ],
  "last_summarized_turn": int,
  "archival_stats": {
    "total_archived": int,
    "last_archived_at": "ISO8601 timestamp"
  }
}
```

### Testing Strategy

**Unit Tests:**
- Each memory tool in isolation
- Core memory CRUD operations
- Summarization with mock LLM responses
- Archival storage and retrieval

**Integration Tests:**
- Full conversation flow with memory management
- Context loading with core_memory injection
- Archival + search round-trip
- Memory tools via AgentRunner

**Performance Tests:**
- Context loading speed with large threads
- Search performance in archival storage
- Embedding generation latency (if used)

**User Acceptance Tests:**
- Supervisor agent manages memory appropriately
- Long conversation remains coherent
- Memory persists across sessions
- Search finds relevant past interactions

## Open Questions

### Technical Decisions

1. **Embeddings: Yes or No?**
   - **Option A:** Use pgvector embeddings for semantic search
     - Pros: High-quality semantic retrieval, finds related concepts
     - Cons: Storage overhead, embedding generation latency, requires pgvector extension
   - **Option B:** Use PostgreSQL full-text search (GIN indexes)
     - Pros: Faster, built-in, no external dependencies
     - Cons: Keyword-based only, misses semantic relationships
   - **Recommendation:** Start with Option B, add Option A as enhancement

2. **Archival Strategy: Copy or Move?**
   - **Option A:** Copy messages to archival, mark original as archived
     - Pros: Preserve ThreadMessage integrity, easier rollback, audit trail
     - Cons: Duplicated storage
   - **Option B:** Move messages (delete from ThreadMessage after archiving)
     - Pros: Reduced storage, cleaner separation
     - Cons: Lose original message IDs, harder to debug, risky
   - **Recommendation:** Option A (copy + flag) - safety first

3. **When to Trigger Summarization?**
   - **Option A:** Agent-initiated (supervisor decides)
     - Pros: Intelligent, context-aware decisions
     - Cons: Relies on agent prompt, may forget
   - **Option B:** Automatic threshold (e.g., every 100 messages)
     - Pros: Guaranteed compression, predictable behavior
     - Cons: May summarize at awkward times, less flexible
   - **Option C:** Hybrid (auto-trigger with agent confirmation)
     - Pros: Best of both worlds
     - Cons: More complex
   - **Recommendation:** Option C - auto-suggest, agent executes

4. **Memory Strategy Per-Agent or Per-Thread?**
   - Current: `memory_strategy` field in Thread table
   - **Question:** Should memory tools be available to all agents or opt-in?
   - **Recommendation:** Opt-in via `allowed_tools` - not all agents need memory management

### Product Questions

1. **User Visibility:**
   - Should users see core_memory in UI?
   - Should users be able to edit core_memory directly?
   - How to display conversation summaries?

2. **Migration Path:**
   - How to handle existing long threads?
   - Automatically summarize on upgrade?
   - Backfill archival storage?

3. **Cost Implications:**
   - Summarization uses LLM tokens (cost per summary)
   - Embeddings generation adds compute cost
   - Archival storage increases database size
   - How to balance cost vs. capability?

4. **Supervisor Behavior:**
   - Should supervisor proactively manage memory for ALL threads?
   - Only manage its own memory?
   - Manage memory for subordinate agents?

### Research Extensions

1. **Cross-Thread Memory:**
   - Should agents learn across different conversation threads?
   - Shared knowledge base for all threads of an agent?
   - Privacy implications?

2. **Memory Compression Techniques:**
   - Beyond summarization: knowledge graphs?
   - Entity extraction and relationship mapping?
   - Automatic tagging and categorization?

3. **Retrieval Strategies:**
   - Pure semantic search vs. hybrid (semantic + keyword + temporal)?
   - Re-ranking retrieved context by relevance?
   - Contextual retrieval (consider current task)?

4. **Memory Forgetting:**
   - Should old memories be deleted/deprecated?
   - Confidence scores on facts (decay over time)?
   - Explicit memory deletion tools?

---

## References

- **MemGPT Paper:** arXiv:2310.08560 - "MemGPT: Towards LLMs as Operating Systems"
- **Zerg Codebase:**
  - `apps/zerg/backend/zerg/models/models.py` - Thread, ThreadMessage, AgentRun models
  - `apps/zerg/backend/zerg/managers/agent_runner.py` - Context loading and execution
  - `apps/zerg/backend/zerg/services/thread_service.py` - Thread persistence helpers
- **Related Zerg Docs:**
  - `connector_aware_agents_prd.md` - Temporal awareness, connector protocols
  - `jarvis_integration.md` - Supervisor agent architecture

---

**Next Steps:**
1. Review and validate this research with team/stakeholders
2. Prioritize phases based on immediate needs
3. Create detailed implementation tickets for Phase 1
4. Prototype core_memory tools with simple test agent
5. Evaluate embeddings vs. full-text search tradeoff with benchmarks