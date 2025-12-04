# LSFS and Semantic Search for Worker Artifacts

**Research Date:** 2025-12-03
**Status:** Future Enhancement - "Later" Priority

---

## Summary

LSFS (LLM-based Semantic File System) from the AIOS project (arXiv:2410.11843) provides a natural language interface for file operations using semantic indexing via vector databases. While promising for future iterations, Zerg's current filesystem-based artifact storage is appropriate for the MVP stage. Semantic search should be considered once we have significant artifact volume and identified user pain points around discovery.

---

## What LSFS/AIOS Provides

### Core Architecture

**AIOS (AI Agent Operating System)** is an OS architecture that embeds LLMs as the core kernel, providing scheduling, context management, and resource allocation for LLM-based agents.

**LSFS (LLM-based Semantic File System)** is a key AIOS component that enables:

1. **Semantic File Retrieval**: Query files by meaning rather than exact paths
   - Example: "Find the marketing reports from last quarter" instead of navigating folder hierarchies
   - Uses vector similarity search over file embeddings

2. **Natural Language File Operations**: Issue file commands via prompts
   - Create, read, update, delete operations through conversational interface
   - Example: "Show me all Python files that handle database connections"

3. **Automatic Content Monitoring**: Track and summarize file changes
   - System watches for modifications
   - Generates summaries of what changed and why

4. **Semantic Version Control**: Rollback files based on content understanding
   - "Revert to the version before we added authentication"
   - Goes beyond git's path-based versioning

### System Call Interface

LSFS provides a semantic layer over traditional file operations:

- `sto_mount`: Mount directories with semantic indexing
- `sto_create_file`: Create files with automatic semantic index generation
- `sto_write`: Write content with embedding updates
- `sto_retrieve`: Semantic search across indexed content
- `sto_rollback`: Content-aware version restoration
- `sto_share`: Generate shareable links with context

### Integration Patterns

LSFS operates at the macro level (comprehensive API) and micro level (vector DB + system calls):

- **Vector Database**: Stores semantic indexes of file contents
- **LLM Core**: Processes natural language requests
- **Action Types**: Chat, tool use, and file operations unified

---

## Path-Based vs Semantic File Access

### Traditional Path-Based Access (Current Zerg Approach)

**How it works:**
```
/data/agents/agent_123/runs/run_456/output.json
/data/workflows/wf_789/execution_012/node_state.json
```

**Strengths:**
- Simple, predictable structure
- Fast direct access by ID
- Easy backup and replication
- Low overhead (no indexing required)
- Transparent debugging (inspect files directly)

**Weaknesses:**
- Requires knowing exact location
- No content-based discovery
- Must traverse hierarchy manually
- Difficult to find "similar" artifacts

### Semantic Search Access (LSFS Approach)

**How it works:**
```
Query: "Find all agent runs that successfully processed email triggers"
→ Vector search returns relevant artifacts regardless of path
```

**Strengths:**
- Natural language queries
- Content-based discovery
- Find related artifacts by meaning
- No need to remember paths

**Weaknesses:**
- Requires embedding generation (cost + latency)
- Vector DB infrastructure needed
- Index maintenance overhead
- More complex debugging

---

## Application to Worker Artifacts

### Current Zerg Architecture

Based on examination of `/Users/davidrose/git/zerg/apps/zerg/backend/zerg/models/models.py`:

**Artifact Storage:**
1. **Agent Runs**: Stored in `AgentRun` model with `summary` field (Text)
2. **Workflow Executions**: `WorkflowExecution` with `log` field (Text)
3. **Node States**: `NodeExecutionState` with `output` field (JSON)
4. **Thread Messages**: `ThreadMessage` with `content` field (Text)

**Current Access Patterns:**
- Direct SQL queries by agent_id, workflow_id, run_id
- Time-based filtering (created_at, updated_at timestamps)
- Status-based queries (phase, result, error fields)
- Relationship traversal (agent → runs → threads → messages)

**No Current Semantic Capabilities:**
- No vector embeddings stored
- No semantic indexing layer
- No content-based search beyond SQL LIKE queries
- No similarity search across artifacts

### How Semantic Search Would Layer On

**Potential Architecture:**

```
┌──────────────────────────────────────┐
│  User Query Interface                │
│  "Show runs that handled calendar    │
│   invites with conflicts"            │
└────────────┬─────────────────────────┘
             │
             v
┌──────────────────────────────────────┐
│  Semantic Query Layer (New)          │
│  - Parse natural language            │
│  - Generate query embedding          │
│  - Vector similarity search          │
└────────────┬─────────────────────────┘
             │
             v
┌──────────────────────────────────────┐
│  Vector Store (New)                  │
│  - Embeddings of summaries/outputs   │
│  - Metadata (agent_id, timestamp)    │
│  - Index for fast retrieval          │
└────────────┬─────────────────────────┘
             │
             v
┌──────────────────────────────────────┐
│  PostgreSQL (Current)                │
│  - Full artifact data                │
│  - Structured metadata               │
│  - Relationships                     │
└──────────────────────────────────────┘
```

**Workflow:**

1. **Indexing Phase** (background process):
   - When AgentRun completes → extract summary + output
   - Generate embedding via OpenAI API (`text-embedding-3-small`)
   - Store in vector DB (pgvector extension or separate Pinecone/Weaviate)
   - Link embedding to run_id in metadata

2. **Query Phase** (user-initiated):
   - User asks: "Which agents handled email attachments this week?"
   - System generates query embedding
   - Vector search returns top-k similar run_ids
   - Fetch full records from PostgreSQL using run_ids
   - Present results with relevance scores

**Use Cases Enabled:**

1. **Cross-Agent Pattern Discovery**
   - "Find all runs where agents encountered rate limiting"
   - "Show executions similar to this failed workflow"

2. **Temporal Context Search**
   - "What did my agents do about the product launch last month?"
   - "Find conversations where users asked about pricing"

3. **Debugging by Similarity**
   - "Find other runs that failed like this one"
   - "Show executions with similar error patterns"

4. **Workflow Archaeology**
   - "When did we last process invoices over $10k?"
   - "Find the run that created this specific output"

---

## Infrastructure Requirements

### Components Needed

1. **Vector Database**
   - **Option A**: pgvector extension on existing PostgreSQL
     - Pros: Single database, simpler ops
     - Cons: Limited scale, slower vector queries
   - **Option B**: Dedicated vector DB (Pinecone, Weaviate, Qdrant)
     - Pros: Better performance, advanced features
     - Cons: Additional service to manage

2. **Embedding Generation**
   - OpenAI `text-embedding-3-small` (1536 dimensions, $0.02/1M tokens)
   - Batch processing for historical artifacts
   - Real-time generation for new runs (adds ~200ms latency)

3. **Indexing Pipeline**
   - Background worker to process completed runs
   - Queue system (existing APScheduler could work)
   - Error handling for embedding failures

4. **Query Interface**
   - API endpoint: `POST /api/v1/search/semantic`
   - Frontend search component
   - Result ranking/filtering UI

5. **Monitoring & Observability**
   - Track embedding generation costs
   - Index freshness metrics
   - Query latency monitoring

### Cost Estimates

**For moderate usage (1000 runs/day, 500 tokens avg per run):**
- Embedding cost: 500K tokens/day × $0.02/1M = $0.01/day = $3.65/year
- Vector DB: pgvector (free) or Pinecone starter (~$70/month)

**Storage:**
- 1536 dimensions × 4 bytes = 6KB per embedding
- 1M artifacts × 6KB = ~6GB vector storage

---

## Priority Assessment: Now vs Later

### Why This is a "Later" Priority

**Current State:**
1. **Low Artifact Volume**: Early MVP stage, limited historical data
2. **Direct Access Works**: Current ID-based queries are sufficient
3. **No User Pain Points**: No evidence users struggle to find artifacts
4. **Infrastructure Overhead**: Vector DB adds complexity for minimal benefit
5. **Cost Before Value**: Embedding costs without proven use case

**Prerequisites Before Implementation:**
1. **Scale Threshold**: 10K+ historical runs worth searching
2. **User Demand**: Explicit requests for "find similar runs" features
3. **Search Use Cases**: Identified patterns that SQL can't handle
4. **Product Maturity**: Core platform stable, team capacity available

### When to Revisit

**Triggering Conditions:**
1. Users repeatedly ask: "How do I find that run where...?"
2. Support burden from artifact discovery issues
3. Internal ops team needs pattern analysis across runs
4. Competitive pressure (other agent platforms have this)
5. Historical corpus reaches 100K+ artifacts

**Estimated Timeline:** 6-12 months post-MVP launch

---

## Future Implementation Sketch

### Phase 1: Foundation (2-4 weeks)

1. **Add pgvector Extension**
   ```sql
   CREATE EXTENSION vector;
   ALTER TABLE agent_runs ADD COLUMN embedding vector(1536);
   CREATE INDEX ON agent_runs USING ivfflat (embedding vector_cosine_ops);
   ```

2. **Background Embedding Worker**
   - Celery task or APScheduler job
   - Process completed runs: summary + output → embedding
   - Update `embedding` column

3. **Basic Search API**
   ```python
   @router.post("/api/v1/search/semantic")
   async def semantic_search(query: str, limit: int = 10):
       # Generate query embedding
       query_vec = await generate_embedding(query)
       # Vector similarity search
       results = db.query(AgentRun).order_by(
           AgentRun.embedding.cosine_distance(query_vec)
       ).limit(limit).all()
       return results
   ```

### Phase 2: UX Integration (2-3 weeks)

1. **Search Bar in Dashboard**
   - Natural language input
   - Live results with relevance scores
   - Filters: date range, agent, status

2. **"Similar Runs" Feature**
   - Show related executions on run detail page
   - Click to explore similar patterns

3. **Debugging Assistant**
   - Failed run page shows similar failures
   - Pattern detection across error types

### Phase 3: Advanced Features (Ongoing)

1. **Hybrid Search**: Combine vector + SQL filters
2. **Temporal Embeddings**: Time-aware similarity
3. **Multi-Modal**: Embed tool outputs, structured data
4. **Clustering**: Group similar runs automatically
5. **Summarization**: LLM-generated "what happened this week"

### Estimated Effort

- **Phase 1**: 2-4 weeks (1 engineer)
- **Phase 2**: 2-3 weeks (1 engineer)
- **Phase 3**: Incremental (ongoing)

**Total to MVP**: ~6 weeks for basic semantic search capability

---

## References

- **Paper**: "From Commands to Prompts: LLM-based Semantic File System for AIOS"
  - arXiv: [2410.11843](https://arxiv.org/abs/2410.11843)
- **AIOS Project**: [github.com/agiresearch/AIOS](https://github.com/agiresearch/AIOS)
- **LSFS Repository**: [github.com/agiresearch/AIOS-LSFS](https://github.com/agiresearch/AIOS-LSFS)
- **Related**: AIOS documentation at [docs.aios.foundation](https://docs.aios.foundation/aios-docs)

---

## Decision

**Recommendation**: DEFER to post-MVP phase (6-12 months out)

**Rationale:**
1. Current filesystem + SQL approach is sufficient for MVP scale
2. No identified user pain points that semantic search would solve
3. Infrastructure investment not justified by current artifact volume
4. Team should focus on core agent functionality and user acquisition
5. Can adopt later with minimal refactoring (additive change)

**Action Items:**
- [ ] Monitor for triggering conditions (user requests, scale issues)
- [ ] Revisit decision at 10K+ artifact milestone
- [ ] Consider during Q2 2026 planning if platform gains traction
- [ ] Keep watching AIOS/LSFS project for production-ready patterns
