# Super Siri - Summary Extraction Implementation Handoff

**Date:** December 2024
**Next Task:** Phase 2.5 - Summary Extraction
**Project:** Zerg Super-Siri (Supervisor/Worker Architecture)

---

## Context

Building "Super Siri" - a unified AI assistant with supervisor/worker architecture. Think personal intern that:

- Handles simple requests directly
- Delegates complex work to disposable workers
- Maintains context across all interactions
- Connects dots across domains

---

## What's Built (Complete ✅)

### Milestones 1-3: Foundation

- **PostgresSaver**: Durable checkpointing (replaces MemorySaver)
- **WorkerArtifactStore**: Filesystem persistence for worker outputs at `/data/swarmlet/workers/`
- **WorkerRunner**: Executes agents as disposable workers
- **Supervisor Tools**: 6 tools (spawn_worker, list_workers, read_worker_result, etc.)
- **Supervisor Agent**: System prompt + seed script

**Location:** `/Users/davidrose/git/zerg/apps/zerg/backend/`

**Tests:** 67/67 passing

---

## Current Problem: Context Window Explosion

```python
# Without summaries
list_workers(limit=50)
# = 50 workers × 500 tokens/result = 25,000 tokens
# ❌ Cannot fit in supervisor context window

# With summaries (needed)
list_workers(limit=50)
# = 50 workers × 30 tokens/summary = 1,500 tokens
# ✅ Supervisor can scan hundreds of workers
```

**Insight:** Summary isn't metadata - it's a **compression layer** that enables scale.

---

## Next Task: Phase 2.5 - Summary Extraction

### What to Implement

**File:** `zerg/services/worker_runner.py`

Add summary extraction to `run_worker()` method:

```python
async def run_worker(...) -> WorkerResult:
    # 1. Execute worker (already works)
    result = await runner.run_thread(db, thread)
    natural_output = extract_final_message(result)

    # 2. Save canonical result (already works)
    artifact_store.save_result(worker_id, natural_output)

    # 3. Mark complete FIRST (system status) (already works)
    artifact_store.complete_worker(worker_id, status="success")

    # 4. Extract summary (NEW)
    summary, summary_meta = await self._extract_summary(task, natural_output)

    # 5. Update metadata with summary (NEW)
    artifact_store.update_summary(worker_id, summary, summary_meta)

    # 6. Return with summary (UPDATE)
    return WorkerResult(
        worker_id=worker_id,
        result=natural_output,  # Full text
        summary=summary,         # Compressed (NEW FIELD)
        ...
    )
```

### Summary Extraction Implementation

```python
async def _extract_summary(self, task: str, result: str) -> tuple[str, dict]:
    """Extract compressed summary for context efficiency.

    Falls back to truncation if LLM fails.

    Returns:
        (summary, summary_meta) tuple
    """
    SUMMARY_VERSION = 1
    MAX_CHARS = 150

    try:
        # LLM extraction
        prompt = f"""Task: {task}
Result: {result[:1000]}

Provide a {MAX_CHARS}-character summary focusing on outcomes, not actions.
Be factual and concise. Do NOT add status judgments.

Example: "Backup completed 157GB in 17s, no errors found"
"""

        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50,
            timeout=5.0
        )

        summary = response.choices[0].message.content.strip()
        if len(summary) > MAX_CHARS:
            summary = summary[:MAX_CHARS-3] + "..."

        return summary, {
            "version": SUMMARY_VERSION,
            "model": "gpt-4o-mini",
            "generated_at": datetime.now(timezone.utc).isoformat()
        }

    except Exception as e:
        # Fallback: truncation
        logger.warning(f"Summary extraction failed: {e}")
        summary = result[:MAX_CHARS-3] + "..." if len(result) > MAX_CHARS else result

        return summary, {
            "version": SUMMARY_VERSION,
            "model": "truncation-fallback",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "error": str(e)
        }
```

### Update WorkerArtifactStore

**File:** `zerg/services/worker_artifact_store.py`

Add method:

```python
def update_summary(self, worker_id: str, summary: str, summary_meta: dict) -> None:
    """Update worker metadata with extracted summary.

    Called after worker completes. Safe to fail.
    """
    meta_path = self._get_worker_path(worker_id) / "metadata.json"
    metadata = json.loads(meta_path.read_text())

    metadata["summary"] = summary
    metadata["summary_meta"] = summary_meta

    meta_path.write_text(json.dumps(metadata, indent=2))

    # Update index
    self._update_index_entry(worker_id, {"summary": summary})
```

Add to top of file (invariants comment):

```python
"""Worker Artifact Store – filesystem persistence.

INVARIANTS:
- result.txt is canonical. Never delete or auto-truncate.
- metadata.json contains derived views (summaries, extracted fields).
- Derived data MUST be recomputable from canonical artifacts.
- System decisions (status) never depend on LLM-generated summaries.
"""
```

### Update WorkerResult Dataclass

**File:** `zerg/services/worker_runner.py`

```python
@dataclass
class WorkerResult:
    worker_id: str
    status: str
    result: str       # Full natural language result
    summary: str      # NEW: Compressed summary (150 chars)
    error: str | None = None
    duration_ms: int = 0
```

### Update list_workers() Tool

**File:** `zerg/tools/builtin/supervisor_tools.py`

Update the tool implementation to return summaries in the formatted output:

```python
def list_workers(limit: int = 20, status: str = None, since_hours: int = None) -> str:
    # ... existing code to get workers ...

    output = f"Recent workers (showing {len(workers)}):\n\n"
    for worker in workers:
        # Use summary if available, otherwise truncate task
        summary = worker.get("summary", worker["task"][:150])
        output += f"- {worker['worker_id']} [{worker['status'].upper()}]\n"
        output += f"  {summary}\n\n"

    output += "Use read_worker_result(worker_id) for full details."
    return output
```

Update docstring:

```python
"""List recent worker executions with SUMMARIES ONLY.

Returns compressed summaries for scanning. To get full details,
call read_worker_result(worker_id).

This prevents context overflow when scanning 50+ workers.
"""
```

---

## Tests Required

**File:** `tests/test_worker_summary.py` (new)

```python
async def test_summary_extraction_success():
    """Summary extracted and saved to metadata."""
    result = await runner.run_worker(db, task="Check disk", ...)

    assert result.summary is not None
    assert len(result.summary) <= 150
    assert result.summary != result.result  # Compressed, not full

    metadata = store.get_worker_metadata(result.worker_id, owner_id)
    assert metadata["summary"] == result.summary
    assert metadata["summary_meta"]["version"] == 1

async def test_summary_extraction_fallback():
    """If summary fails, falls back to truncation."""
    # Mock OpenAI to fail
    with patch('...', side_effect=Exception("API down")):
        result = await runner.run_worker(db, task="Test", ...)

    # Worker still succeeds
    assert result.status == "success"
    # Summary uses fallback
    assert result.summary is not None
    assert "truncation-fallback" in store.get_worker_metadata(...)["summary_meta"]["model"]

async def test_list_workers_returns_summaries_only():
    """list_workers returns summaries, not full results."""
    # Create workers with known results
    result1 = await runner.run_worker(db, task="Task 1", ...)

    # Call list_workers tool
    output = list_workers(limit=10)

    # Should have summary
    assert result1.summary in output
    # Should NOT have full result
    assert result1.result not in output
```

---

## Key Principles (Don't Forget)

1. **result.txt is canonical** - Never depend on summary for system logic
2. **status is system truth** - Exit codes/exceptions, NOT LLM opinion
3. **summary can fail** - Graceful degradation to truncation
4. **list_workers() contract** - MUST return summaries only, never full results
5. **Version summaries** - Track prompt version, model, timestamp

---

## Implementation Checklist

- [ ] Add invariants comment to `worker_artifact_store.py`
- [ ] Add `update_summary()` method to `WorkerArtifactStore`
- [ ] Add `summary` field to `WorkerResult` dataclass
- [ ] Implement `_extract_summary()` in `WorkerRunner`
- [ ] Update `run_worker()` to extract and save summary
- [ ] Update `list_workers()` tool to return summaries
- [ ] Update `list_workers()` docstring
- [ ] Create `tests/test_worker_summary.py`
- [ ] Run all tests: `uv run pytest tests/test_worker*.py -v`

**Estimated effort:** 6-7 hours

---

## Verification

After implementation:

```bash
# All tests pass
cd /Users/davidrose/git/zerg/apps/zerg/backend
uv run pytest tests/test_worker*.py -v

# Manually verify
uv run python examples/supervisor_tools_demo.py
# Check that workers have summaries in metadata
cat /data/swarmlet/workers/*/metadata.json | grep summary
```

---

## References

- **Spec:** `/Users/davidrose/git/zerg/docs/specs/super-siri-architecture.md`
- **Research:** `/Users/davidrose/git/zerg/docs/research/` (5 docs covering MemGPT, Letta, LSFS)
- **Code:** `/Users/davidrose/git/zerg/apps/zerg/backend/`

---

## Why Summary Extraction Matters

From the spec:

> Without summaries, the supervisor cannot scan 50+ workers without hitting context limits. Summary extraction is NOT metadata - it's a **compression layer** that makes the architecture scale. Full result.txt remains the source of truth.

LLM cost is cheap in 2025. We use multiple LLM calls:

- Worker execution (gpt-4o-mini): ~$0.005
- Summary extraction (gpt-4o-mini): ~$0.00001
- Supervisor synthesis (gpt-4o): ~$0.02

Total: ~$0.025/complex task. The summary extraction is negligible but enables scanning 100+ workers.

---

_Ready to implement Phase 2.5_
