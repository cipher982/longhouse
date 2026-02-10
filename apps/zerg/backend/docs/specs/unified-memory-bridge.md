# Harness Simplification & Commis-to-Timeline Unification

**Status:** Active
**Owner:** David Rose
**Created:** 2026-02-09
**Replaces:** Unified Memory Bridge spec (read-through adapter approach abandoned)

## Why

Longhouse accumulated ~55K LOC of custom agent harness (in-process ReAct loop, 31 builtin tools, skills system, prompt assembly, etc.) across multiple pivots. This competes with what Claude Code / Codex / Gemini CLI already do better with teams of hundreds.

Meanwhile, commis output doesn't appear in the agent timeline — Longhouse's own work is invisible to its own product.

## Decision

1. **All commis become CLI agent subprocesses** (workspace mode). Standard mode (in-process) is deprecated.
2. **Commis sessions are ingested into the agent timeline** via direct `AgentsStore.ingest_session()` call (same store as the `/api/agents/ingest` endpoint).
3. **Oikos becomes a thin coordinator** — direct LLM API calls for conversation, `spawn_commis` for real work. No custom tool execution engine.
4. **The legacy harness is removed incrementally** — ~25K LOC of dead code cleared over time.
5. **Semantic search added to Longhouse** — embeddings on ingest, replaces Life Hub MCP dependency.

## What Longhouse Owns vs What CLI Agents Own

| Longhouse | CLI Agents (Claude Code, Codex, etc.) |
|-----------|---------------------------------------|
| Orchestration (spawn, cancel, monitor) | The agent loop |
| Job queue + workspace isolation | Tool execution (file edit, bash, MCP) |
| Timeline (unified searchable archive) | Context management + prompt caching |
| Search (FTS5 + semantic) | Streaming + error recovery |
| Resume (pick up any session) | The entire harness |
| Always-on infrastructure | — |
| Runner coordination | — |

## Phase 1: Commis → Timeline Unification

**Goal:** When a commis runs, its session appears in the timeline — indistinguishable from a shipped terminal session.

### Changes

1. **Post-execution ingest in `commis_job_processor.py`:**
   - After workspace mode hatch subprocess completes, find the session JSONL it produced
   - Push it through `AgentsStore.ingest_session()` (same path as shipper)
   - Tag with `environment=commis` or `source=longhouse` for filtering

2. **Verify workspace mode produces JSONL:**
   - `hatch` wraps Claude Code which writes to `~/.claude/projects/`
   - The workspace is isolated, so the JSONL lands in the workspace's Claude state dir
   - Need to confirm path and wire ingestion

3. **Commis timeline integration:**
   - Timeline UI shows commis sessions alongside shipped sessions
   - Filter option to show/hide commis vs terminal sessions
   - Session detail links back to the commis job for context

### Files
- `services/commis_job_processor.py` — add post-execution ingest
- `services/cloud_executor.py` — capture session JSONL path from hatch output
- `services/agents_store.py` — may need minor changes for commis metadata

## Phase 2: Deprecate Standard Mode

**Goal:** Remove the in-process execution path. All commis use workspace mode (CLI agents).

### What becomes dead code
- `services/commis_runner.py` (1,051 LOC) — ✅ removed 2026-02-09
- `managers/fiche_runner.py` (974 LOC) — message assembly + LLM dispatch
- `managers/message_array_builder.py` (523 LOC) — prompt construction
- `managers/prompt_context.py` (452 LOC) — context builder
- `services/oikos_react_engine.py` (1,483 LOC) — the ReAct loop itself
- `tools/` infrastructure: registry, lazy binder, catalog, unified_access, tool_search (~4K LOC)
- `skills/` (14 files) — skill loading pipeline
- `callbacks/` — token streaming callbacks
- `prompts/` — prompt templates
- Standard mode services: commis_resume, roundabout_monitor, commis_artifact_store, etc. (~6.5K LOC)

### What stays (not dead code)
- `tools/builtin/` (~60 tools, ~9.5K LOC) — **kept as modular toolbox**. Each agent configured with a subset. Tools are product features (email, Slack, GitHub, memory, sessions, etc.), not harness code.

### Migration steps
1. Make workspace mode the default (and only) execution mode for commis
2. Gate standard mode behind `LEGACY_STANDARD_MODE=1` env var (escape hatch)
3. Update all tests that use standard mode to use workspace mode
4. Remove standard mode code paths once stable
5. Remove dead tool/skills/callback code incrementally

### What stays
- `services/commis_job_processor.py` — job queue consumer (refactored)
- `services/commis_job_queue.py` — job queue
- `services/workspace_manager.py` — workspace isolation
- `services/cloud_executor.py` — hatch subprocess spawning
- All DB models, CRUD, auth, routing
- Agent timeline (agents_store, agents router)
- Thread/run models (for Oikos conversation history)

## Phase 3: Slim Oikos

**Goal:** Oikos becomes a thin conversation coordinator, not an agent harness.

### Oikos architecture: simple loop + configurable toolbox

Oikos uses a simple `while` loop (`llm.call(messages + tools) → execute tools → repeat`) with a configured subset of the modular toolbox. No ReAct engine, no tool registry, no skills system.

**Oikos tool subset (configurable):**
- Commis lifecycle: spawn, list, check, cancel, wait
- Session discovery: search, grep, filter, detail
- Memory: save, search, list, forget + embeddings search
- Communication: contact_user, send_email, send_sms, send_imessage, slack, discord
- Web: web_search, web_fetch, http_request
- Infrastructure: runner_list, runner_exec, ssh_exec
- Knowledge: knowledge_search
- Internal: task CRUD, connector status
- (User-configurable: agents can have different tool subsets)

**Oikos does NOT need:**
- Custom tool registry / lazy binder / catalog / unified_access
- Skills loading system
- Token streaming callbacks
- Message array builder with cache optimization
- ReAct prompt templates

### Architecture
```
User message → Oikos (simple loop + toolbox subset)
  → Quick action: Oikos handles directly (send email, search sessions, etc.)
  → Complex task: spawn_commis → CLI agent does multi-step work
  → Question about past work: search_sessions / memory_search
  → Conversation: direct LLM response
```

### The "infinite thread" implementation
- Oikos is a single permanent thread per user
- Old messages are pruned/summarized to maintain context window
- Memory system persists key facts across pruning
- This is a product design problem, not an agent harness problem

## Phase 4: Semantic Search

**Goal:** Replace Life Hub MCP `recall`/`search_agent_logs` with Longhouse-native semantic search.

### Approach
- Compute embeddings on session event ingest (background or sync)
- Store in SQLite via sqlite-vec (optional dependency: `pip install longhouse[semantic]`)
- Add `semantic_search_sessions` tool for Oikos
- FTS5 remains the default; semantic is an enhancement
- No pgvector dependency; hosted can use it optionally

### David-specific: Historical Backfill
- One-time script: pull sessions from Life Hub API → push to Longhouse `/api/agents/ingest`
- Run once, not an ongoing bridge
- Verify counts match, then stop using Life Hub for agent memory

## Risks

- Workspace mode is slower to start than in-process (subprocess + git clone overhead)
- hatch/Claude Code may not be installed on all user machines
- Session JSONL format may vary across CLI providers
- Removing the harness may break Oikos features that depend on builtin tools

## Success Criteria

1. Commis sessions appear in timeline within 30s of completion
2. Standard mode fully removed, no regression in commis functionality
3. Oikos works with only: LLM API, spawn_commis, session tools, contact_user
4. Semantic search returns relevant results for "find where I did X"
5. Total backend LOC reduced by ~20K+
