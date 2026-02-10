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

### First-Principles Findings (2026-02-10)

1. **Oikos prompt/tool contract drifted from runtime reality.**
   - Prompt/docs still frame Oikos as an ops assistant that spawns `spawn_commis` for server checks.
   - Runtime is workspace CLI delegation, and legacy `spawn_commis` semantics are ambiguous under workspace-only execution.

2. **Commis backend choice is implicit, not user-intent driven.**
   - Hatch already supports multiple backends (`claude`/`codex`/`gemini`/`zai`), but Oikos does not expose a clean intent contract for selecting one.

3. **Oikos still carries pre-pivot harness complexity.**
   - `fiche_runner` + `oikos_react_engine` + lazy tool infra are heavier than needed for a coordinator role.

### Target Oikos Contract

Oikos chooses one of three paths per user message:

1. **Direct response** — no tools, just answer.
2. **Quick tool action** — Oikos executes direct tools (session search, memory, web, messaging).
3. **CLI delegation** — Oikos spawns a commis (Claude/Codex/Gemini/etc.) for multi-step work.

This keeps Oikos as a router/coordinator, not a second agent harness.

### 3a: Dispatch Contract (Keyword + Intent)

Add an explicit dispatch rule set:

- If user specifies an agent/backend (e.g. "use Claude Code", "run this with Codex", "do this in Gemini"), Oikos honors it.
- If user specifies a git repo, Oikos uses repo workspace delegation.
- If user requests complex work without repo context, Oikos uses scratch delegation (ephemeral workspace) instead of pretending it can run ops inline.

**Backend mapping (target):**
- `claude` intent → `bedrock/claude-sonnet` (or configured Claude default)
- `codex` intent → `codex/gpt-5.2` (or configured Codex default)
- `gemini` intent → `gemini/gemini-pro` (or configured Gemini default)
- No explicit intent → configured default backend/model

Persist backend choice on the commis job metadata so timeline + tooling can filter/audit by backend.

### 3b: Delegation Modes (Repo vs Scratch)

Define delegation modes explicitly:

- **Repo workspace mode**
  - Requires `git_repo`
  - Uses `WorkspaceManager` clone/branch flow
  - Captures diff artifacts

- **Scratch workspace mode**
  - No `git_repo` required
  - Creates ephemeral working directory for CLI agent execution
  - Intended for analysis/research/ops-style tasks where no repo clone is needed

This removes the current ambiguity where Oikos prompt examples and runtime constraints do not match.

### 3c: Tool Contract Cleanup

- Make `spawn_workspace_commis` the canonical delegation tool in prompt + docs.
- Keep `spawn_commis` only as a compatibility alias with explicit semantics (no "standard mode" behavior).
- Remove deprecated execution-mode vocabulary (`standard`, `cloud`, `local`) from user-facing docs.
- Keep `wait_for_commis` as explicit opt-in blocking behavior; default remains async inbox.

### 3d: Loop + Infra Simplification

Oikos should run a simple loop:

`llm.call(messages + tools) -> execute tools -> repeat`

No custom registry/lazy binder/catalog stack, no skills loader, no bespoke ReAct scaffolding.

### 3e: Prompt Contract Refresh

Update Oikos prompt templates so they reflect actual product direction:

- Oikos is orchestration-first, not "server shell proxy."
- Tool examples match current tool schema (no non-existent args like `wait=True`).
- Delegation guidance emphasizes backend intent + repo/scratch mode selection.

### 3f: "Infinite Thread" Handling

- Keep one long-lived Oikos thread per user.
- Summarize/prune old turns for token budget.
- Persist durable memory separately (memory tools + search), not by retaining full raw transcript forever.

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
