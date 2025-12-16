# Swarmlet Knowledge Sync Specification

**Version:** 1.0
**Date:** December 2024
**Status:** Draft
**Author:** David Rose + Claude

---

## Executive Summary

Swarmlet agents need access to user-specific knowledge (infrastructure docs, project details, operational runbooks) without requiring users to maintain rigid schemas or dumping entire knowledge bases into every prompt.

This spec defines a **phased approach** to knowledge sync:

- **Phase 0:** Simple URL-based document fetch + search tool
- **Phase 1:** Multiple source types + basic compiled view
- **Phase 2:** AI-powered compiler + self-healing aliases
- **Phase 3:** Full provenance tracking + enterprise features

Each phase is independently useful and builds on the previous.

---

## Table of Contents

1. [Goals & Non-Goals](#1-goals--non-goals)
2. [Core Concepts](#2-core-concepts)
3. [Phase 0: URL Fetch + Search](#3-phase-0-url-fetch--search)
4. [Phase 1: Multi-Source + Basic Index](#4-phase-1-multi-source--basic-index)
5. [Phase 2: AI Compiler + Learned Aliases](#5-phase-2-ai-compiler--learned-aliases)
6. [Phase 3: Full Provenance + Enterprise](#6-phase-3-full-provenance--enterprise)
7. [Runtime Agent Behavior](#7-runtime-agent-behavior)
8. [Security & Credentials](#8-security--credentials)
9. [Data Models](#9-data-models)
10. [Example: HDR Daily Digest](#10-example-hdr-daily-digest)

---

## 1. Goals & Non-Goals

### Goals

- Let agents operate from **user-owned knowledge** (repos, docs, URLs) without rigid schemas
- Keep prompts **small** - inject discovery hints, not entire knowledge bases
- Make common lookups **fast** (e.g., "what is cube?") while keeping deep detail on-demand
- Ensure agents **know what they don't know** - ask rather than guess
- Support **multiple sources** with consistent behavior
- Be **auditable** - derived facts traceable to source text (Phase 2+)
- **Dogfood-ready** - works for David's setup AND generalizes to other users

### Non-Goals

- Forcing users to author YAML/ontologies for every object
- Injecting entire knowledge base into prompts every run
- Building a full-featured RAG system from day one
- Replacing existing integrations (Notion, etc.) - complement them

---

## 2. Core Concepts

### Two-Layer Model

```
┌─────────────────────────────────────────────────────────┐
│                    SOURCE LAYER                          │
│  (Human-messy, human-native)                            │
│                                                          │
│  - Markdown files in git repos                          │
│  - Uploaded PDFs/docs                                   │
│  - Raw URLs to documentation                            │
│  - Manual notes (typed or dictated)                     │
└─────────────────────────────────────────────────────────┘
                          ↓
                    [Compiler]
                    (Phase 2+)
                          ↓
┌─────────────────────────────────────────────────────────┐
│                   DISCOVERY LAYER                        │
│  (Small, stable, always loaded)                         │
│                                                          │
│  - catalog.md: what docs exist                          │
│  - hot_terms.md: quick lookup for common nouns          │
│  - tree_summaries.md: navigation aid                    │
│  - aliases.learned.json: self-healing from usage        │
└─────────────────────────────────────────────────────────┘
```

**Phase 0-1:** Discovery layer is minimal or absent; agents search source docs directly.
**Phase 2+:** AI compiler generates discovery layer artifacts.

### Knowledge Sources

Users can add multiple knowledge sources:

| Type          | Description                             | Phase |
| ------------- | --------------------------------------- | ----- |
| `url`         | Fetch markdown/text from a URL          | 0     |
| `git_repo`    | Clone/pull from GitHub/GitLab           | 1     |
| `upload`      | User-uploaded files (PDF, DOCX, images) | 1     |
| `manual_note` | Typed or dictated notes                 | 1     |

Each source syncs on a schedule and produces searchable documents.

---

## 3. Phase 0: URL Fetch + Search

**Goal:** Simplest useful thing. Users point at URLs, agents can search them.

### What Gets Built

1. **KnowledgeSource model** - stores source config per user
2. **URL fetcher** - downloads content, stores in DB
3. **knowledge.search tool** - grep/keyword search over stored content
4. **Prompt preamble** - tells agent how to access knowledge

### Data Model

```python
class KnowledgeSource(Base):
    id: int
    owner_id: int  # FK to users
    source_type: str  # "url" for Phase 0
    name: str  # user-friendly label
    config: JSON  # {"url": "...", "auth_header": "..."}
    sync_schedule: str | None  # cron expression, or null for manual
    last_synced_at: datetime | None
    sync_status: str  # "success" | "failed" | "pending"
    created_at: datetime
    updated_at: datetime

class KnowledgeDocument(Base):
    id: int
    source_id: int  # FK to KnowledgeSource
    owner_id: int  # denormalized for query efficiency
    path: str  # original path/URL
    title: str | None  # extracted or inferred
    content_text: str  # normalized text content
    content_hash: str  # for change detection
    metadata: JSON  # mime type, size, etc.
    fetched_at: datetime
```

### URL Fetcher

```python
async def sync_url_source(source: KnowledgeSource) -> None:
    """Fetch URL content and store as KnowledgeDocument."""
    url = source.config["url"]
    headers = {}
    if auth := source.config.get("auth_header"):
        headers["Authorization"] = auth

    response = await httpx.get(url, headers=headers)
    content = response.text

    # Upsert document
    doc = get_or_create_document(source_id=source.id, path=url)
    doc.content_text = content
    doc.content_hash = hashlib.sha256(content.encode()).hexdigest()
    doc.fetched_at = datetime.utcnow()

    source.last_synced_at = datetime.utcnow()
    source.sync_status = "success"
```

### Knowledge Search Tool

```python
@tool
def knowledge_search(query: str, limit: int = 5) -> list[dict]:
    """Search user's knowledge base for relevant content.

    Args:
        query: Search terms (keywords or phrases)
        limit: Max results to return

    Returns:
        List of matching snippets with source info
    """
    docs = get_user_documents(current_user_id)
    results = []

    for doc in docs:
        # Simple keyword search (upgrade to semantic later)
        if matches := find_matches(doc.content_text, query):
            results.append({
                "source": doc.path,
                "title": doc.title,
                "snippets": matches[:3],  # top 3 matching sections
            })

    return sorted(results, key=relevance_score)[:limit]
```

### Prompt Injection

Add to supervisor/worker prompts:

```
## Knowledge Base

You have access to the user's knowledge base via the `knowledge_search` tool.

When you encounter unfamiliar terms (server names, project names, etc.):
1. Search the knowledge base first
2. If not found, ask the user

Never guess hostnames, IPs, endpoints, or credentials. They must come from:
- Knowledge base search results
- Explicit user input
- Configured secrets/integrations
```

### Sync Schedule

- **On-demand:** User clicks "Sync Now" in dashboard
- **Scheduled:** If `sync_schedule` is set (e.g., `0 * * * *` for hourly)
- **Startup:** Sync stale sources (last_synced > 1 hour) on backend start

### API Endpoints

```
POST /api/knowledge/sources
  Create a new knowledge source

GET /api/knowledge/sources
  List user's knowledge sources

PUT /api/knowledge/sources/{id}
  Update source config

DELETE /api/knowledge/sources/{id}
  Remove source and its documents

POST /api/knowledge/sources/{id}/sync
  Trigger immediate sync

GET /api/knowledge/search?q={query}
  Search across all user's documents (for dashboard/debug)
```

### Dashboard UI

Simple panel in settings:

- List of knowledge sources with sync status
- "Add Source" button (URL input for Phase 0)
- Last synced timestamp
- "Sync Now" button
- Search box for testing

---

## 4. Phase 1: Multi-Source + Basic Index

**Goal:** Support git repos, uploads, and generate a simple catalog.

### Additional Source Types

#### Git Repo Source

```python
config = {
    "repo_url": "https://github.com/user/mytech.git",
    "branch": "main",
    "path_filter": "docs/",  # optional: only sync this subdirectory
    "auth_token": "ghp_...",  # for private repos (stored encrypted)
}
```

Sync process:

1. Clone/pull repo to temp directory
2. Walk files matching filter (`.md`, `.txt`, etc.)
3. Create KnowledgeDocument for each file
4. Clean up temp directory

#### Upload Source

```python
config = {
    "filename": "infrastructure-guide.pdf",
    "storage_ref": "s3://bucket/uploads/...",
}
```

Sync process:

1. On upload, extract text (PDF → text, DOCX → text)
2. Store extracted text as KnowledgeDocument
3. Keep original in blob storage for reference

### Basic Index Generation

Generate a simple `catalog.md` without AI:

```python
def generate_catalog(user_id: int) -> str:
    """Generate a document catalog from user's knowledge docs."""
    docs = get_user_documents(user_id)

    lines = ["# Knowledge Base Catalog", ""]

    for source in group_by_source(docs):
        lines.append(f"## {source.name}")
        for doc in source.documents:
            title = doc.title or doc.path.split("/")[-1]
            preview = doc.content_text[:200].replace("\n", " ")
            lines.append(f"- **{title}** - {preview}...")
        lines.append("")

    return "\n".join(lines)
```

This catalog gets injected into prompts (if small enough) or stored as a searchable doc.

### File Type Support

| Type          | Extraction           | Phase |
| ------------- | -------------------- | ----- |
| `.md`, `.txt` | Direct text          | 0     |
| `.pdf`        | pypdf or pdfplumber  | 1     |
| `.docx`       | python-docx          | 1     |
| `.html`       | BeautifulSoup → text | 1     |
| Images        | OCR (future)         | 2+    |

---

## 5. Phase 2: AI Compiler + Learned Aliases

**Goal:** Use AI to generate smart discovery artifacts and learn from usage.

### Compiled View Artifacts

All stored in DB but presented as virtual "files" to agents:

#### catalog.md

One line per doc with AI-generated summary:

```markdown
# Knowledge Base Catalog

## Infrastructure (mytech repo)

- **AGENTS.md** - Main infrastructure overview, server list, SSH access patterns
- **backups/README.md** - Kopia backup configuration, schedules, troubleshooting
- **operations/umami.md** - Umami analytics setup, standardization status

## Projects

- **hdrpop/operations.md** - HDRPop analytics API, monitoring thresholds
```

#### hot_terms.md

Quick lookup for frequently-referenced terms:

```markdown
# Hot Terms

| Term     | Best Reference                | Summary                                                     |
| -------- | ----------------------------- | ----------------------------------------------------------- |
| cube     | mytech/AGENTS.md#servers      | Home GPU server (100.70.237.79), runs AI workloads, Frigate |
| clifford | mytech/AGENTS.md#servers      | Production VPS on Hetzner, hosts 90% of web apps            |
| hdrpop   | projects/hdrpop/operations.md | HDR photo restoration SaaS, analytics at /api/reports/\*    |
| kopia    | mytech/backups/README.md      | Backup tool, runs nightly to Bremen NAS                     |
```

#### tree_summaries.md

Navigation aid for browsing:

```markdown
# Tree Summaries

- **mytech/** - Infrastructure docs, server configs, operational runbooks
  - **backups/** - Kopia backup configuration and schedules
  - **monitoring/** - Health checks, alerting, cron jobs
  - **operations/** - Deployment guides, service configs
- **projects/** - Per-project operational docs
  - **hdrpop/** - HDR photo restoration app
```

#### unknowns.md

Known gaps requiring user clarification:

```markdown
# Unknown Terms

These terms appear frequently but couldn't be resolved:

- **"prod endpoint"** - Mentioned 5 times, multiple candidates found
- **"the old server"** - No matching documentation
```

#### aliases.learned.json

Self-healing map from actual agent usage:

```json
{
  "cube": {
    "canonical": "mytech/AGENTS.md#cube",
    "confidence": 0.95,
    "last_used": "2024-12-16T10:00:00Z",
    "usage_count": 12
  },
  "hdr": {
    "canonical": "projects/hdrpop/operations.md",
    "confidence": 0.8,
    "last_used": "2024-12-16T09:00:00Z",
    "usage_count": 3
  }
}
```

### AI Compiler Agent

A scheduled Swarmlet agent that rebuilds the discovery layer:

**System prompt:**

```
You are a knowledge compiler. Your job is to read the user's documents
and generate discovery artifacts that help other agents quickly find
information.

For each document, extract:
- A 1-2 sentence summary
- Key entities/terms mentioned (server names, project names, tools)
- Any "access details" (hostnames, IPs, endpoints, commands)

Generate:
1. catalog.md - document index with summaries
2. hot_terms.md - quick lookup table for common terms
3. tree_summaries.md - directory-level summaries
4. unknowns.md - terms that appear but can't be resolved

Rules:
- Every fact must reference which document it came from
- If uncertain, mark as unknown rather than guess
- Prefer specific over vague (IP address > "the server")
```

**Trigger:**

- Nightly scheduled run
- On-demand after source sync
- Event-driven on source changes (webhook/push)

**Cost control:**

- Only reprocess changed documents (content_hash comparison)
- Use cheaper model (gpt-4o-mini) for extraction
- Cache aggressively

### Learned Alias Updates

When an agent resolves a term at runtime:

```python
async def update_learned_alias(user_id: int, term: str, resolved_to: str):
    """Update aliases.learned.json after successful resolution."""
    aliases = get_learned_aliases(user_id)

    if term in aliases:
        aliases[term]["usage_count"] += 1
        aliases[term]["last_used"] = datetime.utcnow()
        # Increase confidence if same resolution
        if aliases[term]["canonical"] == resolved_to:
            aliases[term]["confidence"] = min(0.99, aliases[term]["confidence"] + 0.05)
    else:
        aliases[term] = {
            "canonical": resolved_to,
            "confidence": 0.7,  # start moderate
            "last_used": datetime.utcnow(),
            "usage_count": 1,
        }

    save_learned_aliases(user_id, aliases)
```

---

## 6. Phase 3: Full Provenance + Enterprise

**Goal:** Audit trail, multi-tenant isolation, advanced features.

### Provenance Tracking

Every derived fact stores its source:

```python
class DerivedFact(Base):
    id: int
    owner_id: int
    term: str  # e.g., "cube"
    fact_type: str  # "hostname" | "ip" | "endpoint" | "summary"
    value: str  # e.g., "100.70.237.79"
    source_doc_id: int  # FK to KnowledgeDocument
    source_span: JSON  # {"start": 1234, "end": 1290}
    source_snippet: str  # "cube (100.70.237.79) - Home GPU server"
    confidence: float
    derived_at: datetime
    derived_by: str  # "compiler_v2" | "runtime_search"
```

Agents can query provenance:

```python
@tool
def knowledge_get_provenance(term: str) -> dict:
    """Get source evidence for a derived fact."""
    fact = get_derived_fact(term)
    return {
        "value": fact.value,
        "source": fact.source_doc.path,
        "snippet": fact.source_snippet,
        "confidence": fact.confidence,
    }
```

### Conflict Detection

When compiler finds multiple candidates:

```python
class FactConflict(Base):
    id: int
    owner_id: int
    term: str
    candidates: JSON  # list of {doc_id, value, snippet, confidence}
    resolution: str | None  # user-selected canonical
    resolved_at: datetime | None
```

Dashboard shows conflicts for user resolution.

### Enterprise Features

- **Team knowledge bases** - shared sources with ACLs
- **Knowledge base permissions** - which agents can access which sources
- **Audit log** - who accessed what knowledge when
- **Quality metrics** - track resolution success rate, unknown terms
- **Evaluation harness** - "did the agent resolve 'cube' correctly?"

---

## 7. Runtime Agent Behavior

### Resolver Protocol

When an agent encounters a likely entity (e.g., "cube"):

```
1. CHECK HOT TERMS (Phase 2+)
   → Look in hot_terms.md
   → If found with high confidence, use it

2. CHECK LEARNED ALIASES (Phase 2+)
   → Look in aliases.learned.json
   → If found and recently used, use it

3. SEARCH (All phases)
   → Call knowledge_search("cube")
   → Read top results
   → Extract needed facts

4. DISAMBIGUATE (if multiple candidates)
   → Ask ONE clarifying question
   → "I found multiple servers. Did you mean cube (home GPU) or clifford (prod VPS)?"

5. UPDATE ALIASES (Phase 2+)
   → After successful resolution, update aliases.learned.json
```

### "Never Guess Identifiers" Rule

**Hard requirement across all phases.**

Hostnames, IPs, tokens, endpoints, credentials must come from:

- A knowledge base search result
- An explicit user-provided value
- A configured secret/integration

If not found: **ask**, don't guess.

```python
# In worker prompt
"""
CRITICAL: Never fabricate specific identifiers.

Wrong: ssh root@192.168.1.1  (where did this IP come from?)
Right: knowledge_search("cube ssh") → finds "cube (100.70.237.79)" → ssh cube

Wrong: curl https://api.example.com/endpoint
Right: knowledge_search("hdrpop api") → finds endpoint → curl with found URL

If you cannot find the specific identifier, ASK the user.
"""
```

---

## 8. Security & Credentials

### Source Authentication

| Source Type      | Auth Method       | Storage                    |
| ---------------- | ----------------- | -------------------------- |
| Public URL       | None              | -                          |
| Private URL      | Bearer token      | Encrypted in source.config |
| GitHub (public)  | None              | -                          |
| GitHub (private) | PAT or GitHub App | Encrypted, scoped          |
| GitLab           | PAT or OAuth      | Encrypted, scoped          |

### Secret Handling

**Secrets never stored in knowledge documents.**

If a document contains what looks like a secret:

1. Compiler flags it as potential secret
2. Extract reference only: "API token stored in HDRPOP_ANALYTICS_TOKEN"
3. At runtime, agent fetches from secrets manager

### Multi-Tenant Isolation

- All queries filtered by `owner_id`
- Documents cannot reference other users' sources
- Compiler runs per-user, isolated context

---

## 9. Data Models

### Complete Schema (All Phases)

```python
# Phase 0
class KnowledgeSource(Base):
    __tablename__ = "knowledge_sources"

    id: int = Column(Integer, primary_key=True)
    owner_id: int = Column(Integer, ForeignKey("users.id"), nullable=False)
    source_type: str = Column(String(50), nullable=False)  # url, git_repo, upload, manual
    name: str = Column(String(255), nullable=False)
    config: dict = Column(JSON, nullable=False)  # type-specific config
    sync_schedule: str = Column(String(100), nullable=True)  # cron expression
    last_synced_at: datetime = Column(DateTime, nullable=True)
    sync_status: str = Column(String(50), default="pending")
    sync_error: str = Column(Text, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow)
    updated_at: datetime = Column(DateTime, onupdate=datetime.utcnow)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: int = Column(Integer, primary_key=True)
    source_id: int = Column(Integer, ForeignKey("knowledge_sources.id", ondelete="CASCADE"))
    owner_id: int = Column(Integer, ForeignKey("users.id"), nullable=False)
    path: str = Column(String(1024), nullable=False)  # URL or file path
    title: str = Column(String(512), nullable=True)
    content_text: str = Column(Text, nullable=False)
    content_hash: str = Column(String(64), nullable=False)  # SHA-256
    metadata: dict = Column(JSON, default={})  # mime, size, etc.
    fetched_at: datetime = Column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("source_id", "path", name="uq_source_path"),
    )


# Phase 2+
class CompiledArtifact(Base):
    __tablename__ = "compiled_artifacts"

    id: int = Column(Integer, primary_key=True)
    owner_id: int = Column(Integer, ForeignKey("users.id"), nullable=False)
    artifact_type: str = Column(String(50), nullable=False)  # catalog, hot_terms, etc.
    content: str = Column(Text, nullable=False)
    compiled_at: datetime = Column(DateTime, nullable=False)
    compiler_version: str = Column(String(50), nullable=False)


class LearnedAlias(Base):
    __tablename__ = "learned_aliases"

    id: int = Column(Integer, primary_key=True)
    owner_id: int = Column(Integer, ForeignKey("users.id"), nullable=False)
    term: str = Column(String(255), nullable=False)
    canonical_doc_id: int = Column(Integer, ForeignKey("knowledge_documents.id"))
    canonical_path: str = Column(String(1024), nullable=False)
    confidence: float = Column(Float, default=0.7)
    usage_count: int = Column(Integer, default=1)
    last_used_at: datetime = Column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "term", name="uq_owner_term"),
    )


# Phase 3
class DerivedFact(Base):
    __tablename__ = "derived_facts"

    id: int = Column(Integer, primary_key=True)
    owner_id: int = Column(Integer, ForeignKey("users.id"), nullable=False)
    term: str = Column(String(255), nullable=False)
    fact_type: str = Column(String(50), nullable=False)
    value: str = Column(Text, nullable=False)
    source_doc_id: int = Column(Integer, ForeignKey("knowledge_documents.id"))
    source_span_start: int = Column(Integer, nullable=True)
    source_span_end: int = Column(Integer, nullable=True)
    source_snippet: str = Column(Text, nullable=True)
    confidence: float = Column(Float, nullable=False)
    derived_at: datetime = Column(DateTime, nullable=False)
    derived_by: str = Column(String(100), nullable=False)
```

---

## 10. Example: HDR Daily Digest

### Setup (Phase 0)

1. **Add knowledge source:**

   ```
   Type: url
   Name: HDRPop Operations
   URL: https://raw.githubusercontent.com/cipher982/mytech/main/projects/hdrpop/operations.md
   Auth: (GitHub token for private repo)
   Sync: Daily
   ```

2. **Create the operations doc** (`projects/hdrpop/operations.md`):

   ```markdown
   # HDRPop Operations

   ## Analytics API

   - Endpoint: https://hdrpop.com/api/reports/daily-analytics
   - Auth: X-Analytics-Token header
   - Secret: HDRPOP_ANALYTICS_TOKEN

   ## Key Metrics

   - Funnel: visitors → uploads → results → signups
   - Acquisition sources: Google Ads, ChatGPT referral, Direct
   - Device breakdown: check for mobile vs desktop conversion gaps

   ## Alert Thresholds

   - Upload rate drop >20% day-over-day: investigate
   - Zero signups for 3+ days: check funnel
   - Bot rate >50%: review traffic sources

   ## Notification

   - Daily digest to: david010@gmail.com
   - Urgent alerts: also send to Discord
   ```

3. **Create agent:**
   ```
   Name: HDR Daily Analytics
   Schedule: 0 11 * * * (6am EST)
   Task: Generate daily HDRPop analytics digest and email it to me.
   ```

### Runtime Flow

```
Agent receives task: "Generate daily HDRPop analytics digest"

1. knowledge_search("hdrpop analytics api")
   → Finds operations.md
   → Extracts: endpoint, auth header name, secret reference

2. Get secret HDRPOP_ANALYTICS_TOKEN from secrets manager

3. http_request(
     url="https://hdrpop.com/api/reports/daily-analytics?start=...&end=...",
     headers={"X-Analytics-Token": token}
   )
   → Gets analytics JSON

4. http_request(...) for previous day (comparison)

5. knowledge_search("hdrpop alert thresholds")
   → Finds thresholds section
   → Checks: upload rate change, signup count, bot rate

6. Compose digest email with findings

7. send_email(
     to="david010@gmail.com",
     subject="[INFO] HDRPop Analytics - 2024-12-16",
     body=digest_markdown
   )
```

### With Phase 2 (Hot Terms)

After first run, `aliases.learned.json` contains:

```json
{
  "hdrpop": {
    "canonical": "projects/hdrpop/operations.md",
    "confidence": 0.9
  },
  "hdrpop analytics": {
    "canonical": "projects/hdrpop/operations.md#analytics-api",
    "confidence": 0.85
  }
}
```

Next run skips search, goes straight to the doc.

---

## Implementation Checklist

### Phase 0 (MVP)

- [ ] DB migrations for KnowledgeSource, KnowledgeDocument
- [ ] URL fetcher service
- [ ] `knowledge_search` tool (keyword-based)
- [ ] API endpoints: CRUD for sources, trigger sync, search
- [ ] Dashboard: knowledge sources panel
- [ ] Prompt injection: knowledge base preamble
- [ ] Sync scheduler integration (hourly/daily/on-demand)

### Phase 1

- [ ] Git repo source type + cloner
- [ ] Upload source type + text extraction (PDF, DOCX)
- [ ] Manual note source type
- [ ] Basic catalog generation (non-AI)
- [ ] File type detection and handling

### Phase 2

- [ ] AI compiler agent (scheduled)
- [ ] Compiled artifacts storage
- [ ] hot_terms.md generation
- [ ] tree_summaries.md generation
- [ ] unknowns.md generation
- [ ] Learned aliases tracking
- [ ] Resolver protocol in agent prompts

### Phase 3

- [ ] Full provenance tracking
- [ ] Conflict detection + resolution UI
- [ ] Audit logging
- [ ] Team/shared knowledge bases
- [ ] Quality metrics dashboard
- [ ] Evaluation harness

---

## Open Questions

1. **Semantic search vs keyword search?** Phase 0 uses keyword (grep-style). When to add embeddings/vector search?

2. **Sync frequency limits?** Prevent users from setting 1-minute sync on large repos.

3. **Storage limits?** Max documents per user, max content size per doc.

4. **Compiler cost model?** How to handle users with 1000s of documents - full rebuild too expensive.

5. **Hot term selection criteria?** Frequency in docs? Usage in agent runs? User pinning?

---

_End of Specification_
