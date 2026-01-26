# Jarvis Long-Term Memory (Zerg-Native) Specification

**Version:** 1.0
**Date:** 2026-01-17
**Status:** Draft
**Author:** David Rose + Codex

---

## Executive Summary

Jarvis currently relies on a single long-lived supervisor thread stored in the database. That thread grows without compaction, which eventually breaks recall and inflates prompt size. This spec defines a **Zerg-native long-term memory system** (no LangChain/Agent Builder dependencies) that preserves the best ideas from “memory files” while fitting the existing architecture.

We will implement a **Postgres-backed virtual memory filesystem** (Memory Files), plus a lightweight memory toolset and automatic recall/write policies. Memory will be split into three tiers:

- **Procedural memory**: stable preferences and rules (existing `AgentMemoryKV`).
- **Semantic memory**: facts and docs (existing Knowledge Base).
- **Episodic memory**: short summaries of past interactions (new Memory Files).

The goal is reliable recall for “remember when we worked on X?” without stuffing the entire thread into every prompt.

v1 decisions:
- **Auto summaries** after each supervisor run via async, one-off LLM calls.
- **Embeddings search** from day one, but kept modular and clean (no tangled abstractions).

---

## Table of Contents

1. [Goals & Non-Goals](#1-goals--non-goals)
2. [Current System Snapshot](#2-current-system-snapshot)
3. [Design Overview](#3-design-overview)
4. [Memory Tiers](#4-memory-tiers)
5. [Data Model](#5-data-model)
6. [Tool Surface](#6-tool-surface)
7. [Supervisor Integration](#7-supervisor-integration)
8. [Write Policy](#8-write-policy)
9. [Recall Policy](#9-recall-policy)
10. [Compaction & Retention](#10-compaction--retention)
11. [Security & Privacy](#11-security--privacy)
12. [Rollout Plan](#12-rollout-plan)
13. [Testing Plan](#13-testing-plan)
14. [Risks & Mitigations](#14-risks--mitigations)
15. [Open Questions](#15-open-questions)

---

## 1. Goals & Non-Goals

### Goals
- Provide **long-term recall** without growing prompt size unbounded.
- Keep memory **human-inspectable** and **editable**.
- Support **progressive disclosure**: summaries first, details on demand.
- Integrate cleanly with **existing Zerg architecture** (threads, tools, workers, knowledge base).
- Avoid LangChain/Agent Builder abstractions.

### Non-Goals
- Full semantic RAG from day one.
- Deep, automated “memory reasoning” or self-reflection loops.
- Overhauling the supervisor execution model.
- UI-heavy memory management in v1.

---

## 2. Current System Snapshot

- **Supervisor thread**: One long-lived thread per user in `agent_threads`. All user/assistant messages are persisted and injected into context per run. No summarization/compaction.
- **Worker artifacts**: Stored on filesystem (result.txt, thread.jsonl, tool outputs) and searchable by tools.
- **Knowledge base**: Postgres-backed documents with keyword search.
- **AgentMemoryKV**: Persistent key-value store with tools, currently not enabled for supervisor.

---

## 3. Design Overview

We will add a Zerg-native **Memory Files** system backed by Postgres. Agents interact with memory via file-like tools (ls, read, write, edit, grep). A small, curated set of memory files is injected into the supervisor’s context at run time.

**High-level flow:**

1. **Recall**: At run start, query memory files (and optionally knowledge base) for top matches to the user’s request.
2. **Inject**: Insert a short memory context message into the system prompt.
3. **Run**: Supervisor does its normal work.
4. **Write**: After completion, automatically write a compact episodic summary file (async one-off LLM call).

---

## 4. Memory Tiers

| Tier | Purpose | Storage | Access | Example |
|------|---------|---------|--------|---------|
| Procedural | Stable preferences/rules | `AgentMemoryKV` | `fiche_memory_*` tools | “Always ask for server name” |
| Semantic | Facts/docs | Knowledge Base | `knowledge_search` tool | “Zerg server IPs” |
| Episodic | Past interactions | Memory Files | `memory_*` tools | “HDRPop troubleshooting Jan 5” |

---

## 5. Data Model

### New Table: `memory_files`

```python
class MemoryFile(Base):
    __tablename__ = "memory_files"

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    path = Column(String(512), nullable=False)  # e.g. "episodes/2026-01-17/supervisor-run-123.md"

    title = Column(String(255), nullable=True)
    content = Column(Text, nullable=False)

    tags = Column(MutableList.as_mutable(JSON), nullable=True, default=lambda: [])
    metadata = Column(MutableDict.as_mutable(JSON), nullable=True, default=lambda: {})

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    last_accessed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "path", name="uq_owner_path"),
        Index("ix_memory_owner_path", "owner_id", "path"),
    )
```

### Search Strategy (v1)
- **Embeddings search (primary)** with keyword fallback.
- **Keyword search**: `content ILIKE %query%` + optional tag filters.
- Optional: store `content_hash` for change detection (nice-to-have).

### Embeddings Storage (v1, modular)
We will keep embeddings clean and modular by storing them in a **separate table** and exposing a minimal service layer that can be swapped later.

```python
class MemoryEmbedding(Base):
    __tablename__ = "memory_embeddings"

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    memory_file_id = Column(Integer, ForeignKey("memory_files.id", ondelete="CASCADE"), index=True, nullable=False)

    model = Column(String(128), nullable=False)
    embedding = Column(LargeBinary, nullable=False)  # serialized float32 array

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
```

Notes:
- Keep embeddings decoupled from MemoryFile rows to avoid schema churn.
- Store vectors as serialized bytes (np.ndarray -> bytes) or JSON lists; decide based on Postgres performance.
- The embedding service can be modeled after existing `tools/tool_search.py` (OpenAI embeddings + caching).

### Optional Future Additions (v2+)
- `tsvector` index for Postgres full-text search.

---

## 6. Tool Surface

### Memory Tools (Supervisor + Workers)

- `memory_ls(prefix, limit)` → list files under a path prefix.
- `memory_read(path)` → get file content.
- `memory_write(path, content, tags?, metadata?)` → create or overwrite.
- `memory_edit(path, patch)` → simple line edits (string replace or diff).
- `memory_grep(pattern, prefix?, limit?)` → search content by regex.
- `memory_search(query, tags?, limit?)` → embeddings-first search with keyword fallback + snippets.
- `memory_delete(path)` → delete file.

**Note:** v1 can skip `memory_edit` if speed matters; keep `write` + `read` + `search` as minimum.

---

## 7. Supervisor Integration

### Read-Before-Think (Automatic Recall)
- Add a small function in `AgentRunner` (or `SupervisorService`) to query memory prior to LLM call.
- Inject as an **ephemeral SystemMessage** (similar to connector context).

**Example injected block:**
```
[MEMORY CONTEXT]
- episodes/2026-01-15/zerg-worker-ssh.md
  Summary: “We debugged runner_exec auth failures…”
- projects/hdrpop/status.md
  Summary: “Ads paused due to low ROAS…”
```

### Write-After-Run
- After supervisor completes, generate a short episodic summary via a **one-off async LLM call**.
- Store in `memory_files` under `episodes/YYYY-MM-DD/`.
- If the user explicitly says “remember this,” write immediately (no extra LLM call).

---

## 8. Write Policy

### When to Write
- User explicitly says: “remember this,” “save this,” “keep this in mind.”
- **Always** auto-summarize completed supervisor runs (async background LLM call).
- Explicit project labels (e.g., task contains `project: hdrpop`).

### What to Write
- A short markdown block:

```
# Episode: <Title>
Date: 2026-01-17
Topic: Zerg memory system
Outcome: Decision to implement memory_files table + tools
Refs: thread_id=123, run_id=456
Tags: ["zerg", "memory", "design"]

Summary:
- Key decision …
- Open questions …
```

---

## 9. Recall Policy

### Default Recall
- `memory_search(query, limit=3)` (embeddings-first)
- `knowledge_search(query, limit=3)` (existing)
- Combine into 1 injected context message.

### Progressive Disclosure
- If user asks “more detail,” use `memory_read` or `read_worker_result`.
- Avoid injecting large raw transcripts by default.

---

## 10. Compaction & Retention

### Compaction (v2+)
- Weekly job (Sauron) that merges episodic files into project summaries:
  - `/memories/projects/<project>/summary.md`
- Keep episodic files but allow optional pruning.

### Retention Policy
- Default: keep indefinitely.
- Add only the skeleton hooks (metadata fields) for future TTL/cleanup.

---

## 11. Security & Privacy

- Memory files are scoped by `owner_id` (no cross-user access).
- Tools enforce `owner_id` context (supervisor/worker context only).
- Provide `memory_delete` tool for explicit user control.

---

## 12. Rollout Plan

### Phase 0 (Design + Schema)
- Add `memory_files` table + CRUD
- Add memory tool skeletons

### Phase 1 (MVP)
- Enable `memory_search` (embeddings + fallback), `memory_read`, `memory_write`
- Inject recall context at run start
- Auto summaries after run (async one-off LLM call)
- Manual memory writes (user-triggered)

### Phase 2 (Compaction + UI)
- Periodic rollups
- Optional UI panel for memory files

---

## 13. Testing Plan

- Unit tests for memory CRUD + embedding search + tool responses.
- Integration test: verify memory recall injection for a supervisor run.
- E2E test: “remember X” returns memory summary.

---

## 14. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Memory spam (too many files) | Noise, poor recall | Write policy + compaction |
| Bad memory (incorrect summary) | False recall | Manual “remember” + edit/delete tools |
| Context bloat | Higher cost | Inject only top 1–3 summaries |
| Security leaks | Cross-user access | Strict owner_id scoping |

---

## 15. Open Questions

- What is the exact episodic summary template (fields + formatting)?
- How do we pick top-k memory files for recall (by similarity, recency, or both)?
- Should memory files be visible in UI immediately, or stay system-only until after MVP?
- What should the embedding storage format be (binary vs JSON) for Postgres performance?

---

## Appendix: Relationship to LangChain Ideas

We are **not importing LangChain/DeepAgents code**. We borrow only the concept of a virtual filesystem backed by a durable store, implemented directly in Zerg with our own tools, tables, and policies.
