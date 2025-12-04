# Letta's Filesystem Approach to AI Agent Memory

**Research Date:** 2025-12-03
**Status:** Applicable to Zerg's Swarmlet Architecture

---

## Summary

Letta's Filesystem approach treats agent memory as a hierarchical file/folder structure, enabling agents to navigate documents using simple filesystem tools (open_file, grep_file, search_file). Their benchmarking shows this approach achieves 74.0% accuracy on the LoCoMo benchmark versus 68.5% for specialized graph-based memory tools, proving that agent capability matters more than tool complexity. This directly applies to Zerg's swarmlet architecture where supervisor agents need to browse worker artifacts stored in `/data/swarmlet/workers/`.

---

## Letta's Filesystem Approach

### Core Principles

**1. Documents as Folders**
- Files are organized into folders with descriptive names and descriptions
- Each folder becomes a coherent memory space for related information
- Agents manage context by opening/closing folders dynamically

**2. Simple, Powerful Tools**
Letta exposes three primary filesystem tools to agents:

- **`open_file(file_path)`**: Access specific sections of a file
  - Prevents context window overflow by showing limited portions
  - Agents can navigate through files incrementally

- **`grep_file(file_path, pattern)`**: Search using regular expressions
  - Pattern matching within specific files
  - Efficient for known-structure documents

- **`search_file(folder, query)`**: Semantic (embedding-based) search
  - Files automatically chunked and embedded
  - Find relevant information by meaning, not just keywords
  - Cross-file semantic search within a folder

**3. Automatic Chunking and Embedding**
- Files are chunked automatically for semantic search
- Embeddings generated without agent intervention
- Enables semantic retrieval across large document sets

**4. Context Window Management**
- Only limited portions of files visible at once
- Agents open/close files to manage their context
- Prevents context overflow in long conversations
- Complete transparency into what's loaded

**5. Attachment Model**
- Folders can be attached/detached from agents
- Dynamic memory management based on task needs
- Future-proof for multi-tenant scenarios

### Key Research Finding

**Benchmarking AI Agent Memory: Is a Filesystem All You Need?**

Letta's research compared their filesystem approach against MemGPT's specialized memory tools:

- **Letta Filesystem**: 74.0% accuracy on LoCoMo benchmark
- **MemGPT Graph Variant**: 68.5% accuracy (with specialized tools)

**Conclusion**: Agent capability to use simple tools effectively matters more than memory mechanism complexity. Agents trained on basic filesystem operations can autonomously search, generate queries, and iteratively retrieve information.

---

## Proposed Worker Artifact Structure

### Directory Layout

```
/data/swarmlet/
├── workers/
│   ├── worker-{uuid}/
│   │   ├── metadata.json          # Worker info, status, timestamps
│   │   ├── task.txt               # Original task assignment
│   │   ├── outputs/
│   │   │   ├── report.md          # Primary deliverable
│   │   │   ├── data.json          # Structured output
│   │   │   └── artifacts/         # Supporting files
│   │   ├── logs/
│   │   │   ├── execution.log      # Detailed execution log
│   │   │   └── errors.log         # Error traces if any
│   │   ├── context/
│   │   │   ├── research.md        # Research findings
│   │   │   └── references.json    # External references
│   │   └── tools_used.json        # Record of tools invoked
│   │
│   ├── worker-{uuid}/
│   │   └── ...
│   │
│   └── index.json                 # Master index of all workers
│
└── supervisor/
    ├── strategy.md                # Current delegation strategy
    ├── worker_map.json            # Worker assignments and status
    └── synthesis.md               # Cross-worker synthesis notes
```

### File Specifications

**metadata.json**
```json
{
  "worker_id": "worker-abc123",
  "created_at": "2025-12-03T20:00:00Z",
  "status": "completed",
  "task_type": "research",
  "parent_thread_id": "thread-xyz",
  "completion_time_ms": 45000,
  "supervisor_notes": "High quality output, used for final synthesis"
}
```

**index.json** (Master Index)
```json
{
  "workers": [
    {
      "id": "worker-abc123",
      "task": "Research Letta filesystem approach",
      "status": "completed",
      "path": "workers/worker-abc123",
      "created_at": "2025-12-03T20:00:00Z",
      "tags": ["research", "memory", "letta"]
    }
  ],
  "last_updated": "2025-12-03T21:00:00Z",
  "total_workers": 1,
  "active_workers": 0
}
```

---

## Filesystem Tools for Supervisor

### Tool 1: `list_worker_artifacts`

**Purpose**: Browse available worker outputs

**Signature**:
```python
def list_worker_artifacts(
    status_filter: Optional[str] = None,  # "completed", "failed", "running"
    task_type_filter: Optional[str] = None,  # "research", "analysis", etc.
    limit: int = 10
) -> List[WorkerArtifact]:
    """List available worker artifacts with optional filtering."""
```

**Implementation Notes**:
- Reads `/data/swarmlet/workers/index.json`
- Applies filters to worker list
- Returns metadata for matching workers
- Fast operation (index-based, no file scanning)

**Example Usage**:
```python
# Supervisor: "Show me completed research tasks"
artifacts = list_worker_artifacts(
    status_filter="completed",
    task_type_filter="research"
)
```

### Tool 2: `read_worker_file`

**Purpose**: Read specific files from worker directories

**Signature**:
```python
def read_worker_file(
    worker_id: str,
    file_path: str,  # Relative to worker directory
    max_lines: Optional[int] = None
) -> str:
    """Read a specific file from a worker's artifact directory."""
```

**Implementation Notes**:
- Validates worker_id exists
- Enforces path restrictions (no `../` escapes)
- Supports optional line limiting for large files
- Returns file content as string

**Example Usage**:
```python
# Supervisor: "Read the report from worker-abc123"
report = read_worker_file(
    worker_id="worker-abc123",
    file_path="outputs/report.md"
)
```

### Tool 3: `search_worker_artifacts`

**Purpose**: Search across worker outputs using patterns

**Signature**:
```python
def search_worker_artifacts(
    query: str,
    search_type: str = "grep",  # "grep" or "semantic"
    worker_filter: Optional[List[str]] = None,
    file_pattern: str = "**/*.{md,txt,json}"
) -> List[SearchResult]:
    """Search worker artifacts using grep or semantic search."""
```

**Implementation Notes**:
- **Grep mode**: Uses regex pattern matching (fast)
- **Semantic mode**: Uses embeddings for meaning-based search (future)
- Searches across specified file patterns
- Returns matches with context (file path, line number, surrounding lines)

**Example Usage**:
```python
# Supervisor: "Find all mentions of 'embedding' in worker outputs"
results = search_worker_artifacts(
    query="embedding",
    search_type="grep",
    file_pattern="**/*.md"
)
```

### Tool 4: `summarize_worker_output`

**Purpose**: Get AI-generated summary of worker deliverables

**Signature**:
```python
def summarize_worker_output(
    worker_id: str,
    focus: Optional[str] = None  # Specific aspect to emphasize
) -> str:
    """Generate concise summary of worker's output."""
```

**Implementation Notes**:
- Reads key files (report.md, data.json)
- Uses LLM to generate 2-3 sentence summary
- Optionally focuses on specific aspects
- Caches summaries in metadata for performance

**Example Usage**:
```python
# Supervisor: "Summarize what worker-abc123 found about memory systems"
summary = summarize_worker_output(
    worker_id="worker-abc123",
    focus="memory systems"
)
```

### Tool 5: `compare_worker_outputs`

**Purpose**: Compare outputs from multiple workers

**Signature**:
```python
def compare_worker_outputs(
    worker_ids: List[str],
    comparison_aspect: Optional[str] = None
) -> str:
    """Compare outputs from multiple workers, highlighting differences."""
```

**Implementation Notes**:
- Loads outputs from specified workers
- Uses LLM to identify commonalities and differences
- Returns structured comparison
- Useful for synthesis phase

**Example Usage**:
```python
# Supervisor: "Compare findings from these three research workers"
comparison = compare_worker_outputs(
    worker_ids=["worker-abc", "worker-def", "worker-ghi"],
    comparison_aspect="key findings"
)
```

---

## Integration with Existing Zerg Tools

### Current Tool Infrastructure

**Zerg's Tool Registry** (`zerg/tools/registry.py`):
- **ImmutableToolRegistry**: Thread-safe, built once at startup
- **ToolRegistry**: Mutable singleton for backwards compatibility
- Tools registered from multiple sources (builtin, MCP, custom)

**Tool Categories** (from `zerg/tools/builtin/`):
- Container tools
- HTTP tools
- Datetime tools
- Math tools
- UUID tools
- Connector tools (Slack, Discord, Email, GitHub, Jira, Linear, Notion, SMS, iMessage)

### Integration Strategy

**1. Create New Module**: `zerg/tools/builtin/swarmlet_tools.py`

**2. Follow Existing Pattern**:
```python
"""Swarmlet filesystem tools for supervisor agents."""

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from typing import List, Optional

# Tool implementations here...

TOOLS = [
    StructuredTool.from_function(
        func=list_worker_artifacts,
        name="list_worker_artifacts",
        description="List available worker artifacts with optional filtering"
    ),
    StructuredTool.from_function(
        func=read_worker_file,
        name="read_worker_file",
        description="Read a specific file from a worker's artifact directory"
    ),
    # ... other tools
]
```

**3. Register in `zerg/tools/builtin/__init__.py`**:
```python
from zerg.tools.builtin.swarmlet_tools import TOOLS as SWARMLET_TOOLS

BUILTIN_TOOLS = (
    # ... existing tools
    + SWARMLET_TOOLS
)
```

**4. Tool Allowlist Configuration**:
- Supervisor agents get `swarmlet_*` tool pattern in `allowed_tools`
- Worker agents do NOT get swarmlet tools (they write, don't read)
- Prevents workers from browsing each other's outputs

### Data Persistence

**Storage Location**: `/data/swarmlet/` (container volume mount)

**Model Extensions** (if needed):
- Current `Agent` model already has JSON `config` field
- Store swarmlet-specific config there: `config.swarmlet_role = "supervisor"`
- No schema migration needed initially

**Thread Relationship**:
- Workers are created as child threads of supervisor
- `Thread.agent_state` stores worker_id mapping
- `ThreadMessage.message_metadata` stores artifact references

---

## Implementation Plan

### Phase 1: Core Filesystem Storage (Week 1)

**Goal**: Workers write artifacts to structured directories

**Tasks**:
1. Create directory structure initialization
2. Implement worker artifact writer utility
3. Generate `metadata.json` and `index.json` on worker completion
4. Add integration tests for file generation

**Deliverables**:
- `zerg/services/swarmlet_storage.py` - Storage abstraction
- `/data/swarmlet/` directory structure
- Tests confirming file creation

### Phase 2: Basic Filesystem Tools (Week 2)

**Goal**: Supervisor can read worker outputs

**Tasks**:
1. Implement `list_worker_artifacts` (index-based listing)
2. Implement `read_worker_file` (direct file access)
3. Create `swarmlet_tools.py` module
4. Register tools in builtin registry
5. Add unit tests for each tool

**Deliverables**:
- `zerg/tools/builtin/swarmlet_tools.py`
- Tool registration in `__init__.py`
- Passing unit tests

### Phase 3: Search Capabilities (Week 3)

**Goal**: Supervisor can search across artifacts

**Tasks**:
1. Implement `search_worker_artifacts` (grep mode only)
2. Add file pattern matching logic
3. Return search results with context
4. Add integration tests for search

**Deliverables**:
- Grep-based search functionality
- Search result formatting
- Integration tests

### Phase 4: AI-Enhanced Tools (Week 4)

**Goal**: Supervisor gets summarization and comparison

**Tasks**:
1. Implement `summarize_worker_output` with LLM
2. Add summary caching to metadata
3. Implement `compare_worker_outputs`
4. End-to-end testing with real supervisor agent

**Deliverables**:
- Summary and comparison tools
- Cached summaries in metadata
- E2E supervisor tests

### Phase 5: Semantic Search (Future)

**Goal**: Enable embedding-based semantic search

**Tasks**:
1. Add embedding generation on artifact creation
2. Store embeddings in vector DB or file-based index
3. Implement semantic mode for `search_worker_artifacts`
4. Benchmark performance vs grep search

**Deliverables**:
- Semantic search functionality
- Performance benchmarks
- Documentation on when to use semantic vs grep

---

## Example Usage Scenarios

### Scenario 1: Research Synthesis

**Context**: Supervisor delegates research on 3 different AI memory approaches to 3 workers

**Workflow**:

1. **Supervisor delegates tasks**:
```python
# Supervisor creates 3 worker agents, each researching a different approach
worker1 = create_worker(task="Research Letta's filesystem approach")
worker2 = create_worker(task="Research MemGPT's graph memory")
worker3 = create_worker(task="Research RAG-based approaches")
```

2. **Workers complete and write artifacts**:
```
/data/swarmlet/workers/
  ├── worker-abc123/outputs/report.md (Letta findings)
  ├── worker-def456/outputs/report.md (MemGPT findings)
  └── worker-ghi789/outputs/report.md (RAG findings)
```

3. **Supervisor browses completed work**:
```python
# Supervisor: "Show me completed research tasks"
artifacts = list_worker_artifacts(
    status_filter="completed",
    task_type_filter="research"
)
# Returns: [worker-abc123, worker-def456, worker-ghi789]
```

4. **Supervisor reads individual reports**:
```python
# Supervisor: "Read the Letta research report"
letta_report = read_worker_file(
    worker_id="worker-abc123",
    file_path="outputs/report.md"
)
```

5. **Supervisor compares findings**:
```python
# Supervisor: "Compare all three research approaches"
comparison = compare_worker_outputs(
    worker_ids=["worker-abc123", "worker-def456", "worker-ghi789"],
    comparison_aspect="performance benchmarks"
)
```

6. **Supervisor synthesizes final report**:
```python
# Supervisor uses comparison to write unified analysis
# References specific worker IDs in final output
final_report = synthesize(comparison, source_workers=[...])
```

### Scenario 2: Code Analysis Pipeline

**Context**: Supervisor analyzes a codebase by delegating different components to workers

**Workflow**:

1. **Supervisor creates specialized workers**:
```python
worker1 = create_worker(task="Analyze authentication module")
worker2 = create_worker(task="Analyze database layer")
worker3 = create_worker(task="Analyze API endpoints")
```

2. **Workers produce structured output**:
```
/data/swarmlet/workers/
  ├── worker-auth/
  │   ├── outputs/analysis.json (Security findings)
  │   └── outputs/recommendations.md
  ├── worker-db/
  │   ├── outputs/schema_analysis.json
  │   └── outputs/query_performance.md
  └── worker-api/
      ├── outputs/endpoint_coverage.json
      └── outputs/rest_compliance.md
```

3. **Supervisor searches for security issues**:
```python
# Supervisor: "Find all security vulnerabilities mentioned"
security_issues = search_worker_artifacts(
    query="vulnerability|security|exploit",
    search_type="grep",
    file_pattern="**/*.{md,json}"
)
```

4. **Supervisor generates summary**:
```python
# Supervisor: "Summarize security findings"
for worker in ["worker-auth", "worker-db", "worker-api"]:
    summary = summarize_worker_output(
        worker_id=worker,
        focus="security"
    )
```

5. **Supervisor creates action plan**:
```python
# Supervisor synthesizes findings into prioritized action items
# Cross-references worker outputs in recommendations
```

### Scenario 3: Iterative Refinement

**Context**: Supervisor reviews worker output and requests revision

**Workflow**:

1. **Worker completes initial task**:
```
/data/swarmlet/workers/worker-v1/outputs/report.md
```

2. **Supervisor reviews output**:
```python
report = read_worker_file(
    worker_id="worker-v1",
    file_path="outputs/report.md"
)
# Supervisor determines report needs more detail on section 3
```

3. **Supervisor creates follow-up worker**:
```python
worker_v2 = create_worker(
    task="Expand section 3 of previous report",
    context={
        "previous_worker": "worker-v1",
        "previous_output": report
    }
)
```

4. **Supervisor compares versions**:
```python
comparison = compare_worker_outputs(
    worker_ids=["worker-v1", "worker-v2"],
    comparison_aspect="section 3 depth"
)
```

5. **Supervisor selects best output**:
```python
# Supervisor merges insights from both versions
# Keeps worker-v2's section 3, worker-v1's other sections
```

---

## Design Rationale

### Why Filesystem Over Database?

**Advantages**:
1. **Simplicity**: No ORM, no schema migrations, just files
2. **Transparency**: Easy to inspect/debug with standard tools
3. **Flexibility**: Schema-free JSON for extensibility
4. **Portability**: Artifacts survive container restarts
5. **Tool Familiarity**: Developers understand filesystems
6. **Letta Validation**: Proven approach with benchmark results

**Trade-offs**:
- **Query Performance**: Index.json helps, but not as fast as SQL
- **Concurrency**: Need file locking for writes (not an issue with immutable artifacts)
- **Relationships**: No foreign keys (but workers are hierarchical by design)

**Mitigation**:
- Master index.json provides fast filtering
- Artifacts are write-once, read-many (no locking needed)
- Future: Add SQLite index if query performance becomes issue

### Why Simple Tools Over Complex APIs?

Following Letta's research findings:

1. **Agent Capability > Tool Complexity**: 74% vs 68.5% accuracy
2. **Learning Curve**: Simple tools easier for LLMs to master
3. **Composability**: Simple primitives combine powerfully
4. **Debugging**: Easier to understand tool call sequences
5. **Extensibility**: Simple base enables complex behaviors

### Why Not Semantic Search Initially?

**Rationale**:
1. **MVP Speed**: Grep search is faster to implement
2. **Cost**: Embeddings add inference cost per artifact
3. **Scale**: Semantic search benefits appear at larger scales
4. **Validation**: Need to prove filesystem approach works first
5. **Flexibility**: Can add semantic later without breaking changes

**Future Trigger Points**:
- 100+ worker artifacts per session
- User feedback requesting "find by meaning"
- Grep search becoming slow or inadequate
- Budget allows for embedding costs

---

## Open Questions & Future Research

### 1. Artifact Retention Policy

**Question**: How long do we keep worker artifacts?

**Options**:
- Keep forever (storage grows unbounded)
- Time-based expiration (30 days)
- Size-based LRU eviction
- User-controlled archival

**Recommendation**: Start with forever, add cleanup in Phase 5+

### 2. Cross-Session Artifact Reuse

**Question**: Can supervisors reference artifacts from previous sessions?

**Implications**:
- Richer context across conversations
- Enables learning from past work
- Requires artifact addressing scheme

**Recommendation**: Future feature, requires session-level indexing

### 3. Worker Collaboration

**Question**: Should workers be able to read each other's outputs?

**Use Case**: Worker-B builds on Worker-A's research

**Trade-offs**:
- **Pro**: Enables collaborative workflows
- **Con**: Violates clean separation of concerns
- **Con**: Risk of circular dependencies

**Recommendation**: Supervisor mediates, workers don't directly access peers

### 4. Embedding Strategy for Semantic Search

**Options**:
1. **Embed on write**: Generate embeddings when artifact created
2. **Embed on demand**: Generate when first searched
3. **Batch embedding**: Nightly job embeds all artifacts
4. **Hybrid**: Embed hot artifacts, skip cold ones

**Recommendation**: Embed on write for recent artifacts, batch for historical

### 5. Tool Permissions & Security

**Question**: How do we prevent tool misuse?

**Risks**:
- Supervisor reading non-existent worker IDs (handled by validation)
- Path traversal attacks (prevented by path sanitization)
- Large file reads exhausting context (mitigated by max_lines)

**Recommendation**: Implement path validation, size limits, and audit logging

---

## Success Metrics

### Performance Metrics

1. **Tool Response Time**:
   - `list_worker_artifacts`: < 100ms (index read)
   - `read_worker_file`: < 200ms (single file read)
   - `search_worker_artifacts` (grep): < 500ms (multi-file scan)
   - `summarize_worker_output`: < 3s (LLM call)

2. **Storage Efficiency**:
   - Average artifact size: Target < 100KB per worker
   - Index size: < 1MB for 1000 workers

3. **Context Window Savings**:
   - Without tools: Supervisor loads all worker outputs (100KB * 5 = 500KB)
   - With tools: Supervisor loads summaries only (2KB * 5 = 10KB)
   - Savings: 98% context reduction

### Behavioral Metrics

1. **Tool Usage**:
   - % of supervisor sessions using swarmlet tools
   - Average tools called per session
   - Most frequently used tools

2. **Task Completion**:
   - % of multi-worker tasks successfully synthesized
   - Time from delegation to synthesis
   - User satisfaction with final outputs

3. **Error Rates**:
   - File not found errors
   - Invalid worker ID errors
   - Path traversal attempts blocked

### Benchmark Targets (Following Letta)

- **Accuracy**: Target 70%+ on relevant benchmarks
- **Latency**: P95 tool call latency < 1s
- **Adoption**: 80%+ of multi-agent workflows use filesystem tools

---

## References

1. **Letta Blog Post**: "Benchmarking AI Agent Memory: Is a Filesystem All You Need?"
   - URL: https://www.letta.com/blog/benchmarking-ai-agent-memory
   - Key Finding: 74.0% accuracy with simple filesystem vs 68.5% with complex tools

2. **LoCoMo Benchmark**: Long-context memory evaluation benchmark
   - Tests agent ability to maintain and use conversational history
   - Letta's filesystem approach outperformed specialized memory systems

3. **Zerg Codebase References**:
   - `/apps/zerg/backend/zerg/tools/registry.py` - Tool registration
   - `/apps/zerg/backend/zerg/tools/unified_access.py` - Tool resolution
   - `/apps/zerg/backend/zerg/models/models.py` - Data models

4. **Related Research**: LSFS (LLM-based Semantic File System)
   - See `/docs/research/04-lsfs-semantic-search.md`
   - Future semantic search capabilities

---

## Appendix: Code Snippets

### Worker Artifact Writer

```python
"""Utility for workers to write artifacts to filesystem."""

import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional

class WorkerArtifactWriter:
    """Manages writing worker artifacts to structured directories."""

    def __init__(self, base_path: Path = Path("/data/swarmlet")):
        self.base_path = base_path
        self.workers_path = base_path / "workers"
        self.workers_path.mkdir(parents=True, exist_ok=True)

    def create_worker_directory(self, worker_id: str) -> Path:
        """Create directory structure for a new worker."""
        worker_path = self.workers_path / worker_id
        worker_path.mkdir(exist_ok=True)

        # Create subdirectories
        (worker_path / "outputs").mkdir(exist_ok=True)
        (worker_path / "logs").mkdir(exist_ok=True)
        (worker_path / "context").mkdir(exist_ok=True)

        return worker_path

    def write_artifact(
        self,
        worker_id: str,
        file_path: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Path:
        """Write an artifact file for a worker."""
        worker_path = self.workers_path / worker_id
        artifact_path = worker_path / file_path

        # Ensure parent directory exists
        artifact_path.parent.mkdir(parents=True, exist_ok=True)

        # Write content
        artifact_path.write_text(content)

        # Update metadata if provided
        if metadata:
            self._update_metadata(worker_id, metadata)

        return artifact_path

    def finalize_worker(
        self,
        worker_id: str,
        status: str,
        task: str,
        task_type: str,
        tags: list[str] = None
    ):
        """Mark worker as complete and update index."""
        # Write metadata
        metadata = {
            "worker_id": worker_id,
            "created_at": datetime.utcnow().isoformat(),
            "status": status,
            "task_type": task_type,
        }

        metadata_path = self.workers_path / worker_id / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2))

        # Update master index
        self._update_index(worker_id, task, status, task_type, tags or [])

    def _update_index(
        self,
        worker_id: str,
        task: str,
        status: str,
        task_type: str,
        tags: list[str]
    ):
        """Update master index.json with worker info."""
        index_path = self.workers_path / "index.json"

        # Load existing index
        if index_path.exists():
            index = json.loads(index_path.read_text())
        else:
            index = {"workers": [], "total_workers": 0}

        # Add/update worker entry
        worker_entry = {
            "id": worker_id,
            "task": task,
            "status": status,
            "path": f"workers/{worker_id}",
            "created_at": datetime.utcnow().isoformat(),
            "tags": tags,
            "task_type": task_type,
        }

        # Update or append
        existing = next(
            (i for i, w in enumerate(index["workers"]) if w["id"] == worker_id),
            None
        )

        if existing is not None:
            index["workers"][existing] = worker_entry
        else:
            index["workers"].append(worker_entry)
            index["total_workers"] += 1

        index["last_updated"] = datetime.utcnow().isoformat()

        # Write index
        index_path.write_text(json.dumps(index, indent=2))
```

### Example Tool Implementation

```python
"""Example implementation of swarmlet filesystem tools."""

from pathlib import Path
from typing import List, Optional
import json

def list_worker_artifacts(
    status_filter: Optional[str] = None,
    task_type_filter: Optional[str] = None,
    limit: int = 10
) -> str:
    """
    List available worker artifacts with optional filtering.

    Args:
        status_filter: Filter by status (completed, failed, running)
        task_type_filter: Filter by task type (research, analysis, etc.)
        limit: Maximum number of results

    Returns:
        JSON string with matching workers
    """
    index_path = Path("/data/swarmlet/workers/index.json")

    if not index_path.exists():
        return json.dumps({"workers": [], "message": "No workers found"})

    index = json.loads(index_path.read_text())
    workers = index.get("workers", [])

    # Apply filters
    if status_filter:
        workers = [w for w in workers if w.get("status") == status_filter]

    if task_type_filter:
        workers = [w for w in workers if w.get("task_type") == task_type_filter]

    # Limit results
    workers = workers[:limit]

    return json.dumps({
        "workers": workers,
        "total": len(workers),
        "filtered": bool(status_filter or task_type_filter)
    }, indent=2)


def read_worker_file(
    worker_id: str,
    file_path: str,
    max_lines: Optional[int] = None
) -> str:
    """
    Read a specific file from a worker's artifact directory.

    Args:
        worker_id: Worker identifier
        file_path: Relative path to file (e.g., "outputs/report.md")
        max_lines: Optional limit on lines to read

    Returns:
        File contents as string
    """
    # Security: Validate worker_id format
    if not worker_id.startswith("worker-"):
        return json.dumps({"error": "Invalid worker_id format"})

    # Security: Prevent path traversal
    if ".." in file_path or file_path.startswith("/"):
        return json.dumps({"error": "Invalid file_path"})

    worker_path = Path("/data/swarmlet/workers") / worker_id

    if not worker_path.exists():
        return json.dumps({"error": f"Worker {worker_id} not found"})

    full_path = worker_path / file_path

    if not full_path.exists():
        return json.dumps({"error": f"File {file_path} not found"})

    # Read file
    content = full_path.read_text()

    # Apply line limit if requested
    if max_lines:
        lines = content.split("\n")
        content = "\n".join(lines[:max_lines])
        if len(lines) > max_lines:
            content += f"\n\n... ({len(lines) - max_lines} more lines)"

    return content
```

---

**Document Version**: 1.0
**Last Updated**: 2025-12-03
**Next Review**: After Phase 2 implementation
