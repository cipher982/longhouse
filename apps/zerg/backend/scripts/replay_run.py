#!/usr/bin/env python3
"""Golden Run Replay Harness for testing concierge prompt changes.

This script replays a concierge run using a live LLM while mocking tool results.
Mode: "Tier 2" - Live LLM + mocked tools (not deterministic replay).

Key properties:
- **Isolated by default**: runs in a new replay thread so it doesn't pollute the user's
  long-lived concierge thread (important for repeated prompt iteration).
- **Safe by default**: blocks side-effectful tools unless explicitly allowed.

Usage (run from backend directory):
    cd apps/zerg/backend
    uv run python scripts/replay_run.py <course_id>
    uv run python scripts/replay_run.py <course_id> --dry-run

Example:
    uv run python scripts/replay_run.py 42 --dry-run
    uv run python scripts/replay_run.py 42 --cleanup
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import logging
import sys
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from difflib import SequenceMatcher
from contextlib import ExitStack
from pathlib import Path
from typing import Callable

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from zerg.crud import crud
from zerg.database import get_db
from zerg.managers.fiche_runner import FicheRunner
from zerg.models.enums import CourseStatus
from zerg.models.enums import CourseTrigger
from zerg.models.enums import ThreadType
from zerg.models.models import Course
from zerg.models.models import ThreadMessage
from zerg.models.models import CommisJob
from zerg.services.concierge_service import ConciergeService
from zerg.services.commis_artifact_store import CommisArtifactStore
from zerg.tools.unified_access import get_tool_resolver

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class ReplayStats:
    """Track replay statistics for comparison."""

    spawn_commis_calls: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    blocked_tool_calls: int = 0
    blocked_calls_by_tool: dict[str, int] = field(default_factory=dict)
    tool_call_names: list = field(default_factory=list)


def normalize_datetime(dt: datetime | None) -> datetime | None:
    """Normalize a datetime to UTC timezone-aware.

    Handles the timezone mismatch between:
    - Course.started_at (naive datetime)
    - ThreadMessage.sent_at (timezone-aware datetime)

    Args:
        dt: A datetime that may be naive or aware

    Returns:
        Timezone-aware datetime in UTC, or None if input is None
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Assume naive datetimes are UTC (database convention)
        return dt.replace(tzinfo=timezone.utc)
    return dt


class MockedSpawnCommis:
    """Mock spawn_commis that returns cached results from original run."""

    def __init__(self, db: Session, original_course_id: int, stats: ReplayStats, *, match_threshold: float = 0.7):
        self.db = db
        self.original_course_id = original_course_id
        self.stats = stats
        self.match_threshold = match_threshold
        self.artifact_store = CommisArtifactStore()
        self.cached_jobs = self._load_original_commis()
        self.used_job_ids: set[int] = set()

    def _load_original_commis(self) -> dict:
        """Load commis jobs from the original run."""
        jobs = self.db.query(CommisJob).filter(CommisJob.concierge_course_id == self.original_course_id).all()

        cached = {}
        for job in jobs:
            cached[job.id] = {
                "job_id": job.id,
                "task": job.task,
                "model": job.model,
                "status": job.status,
                "commis_id": job.commis_id,
                "error": job.error,
            }

        logger.info(f"Loaded {len(cached)} cached commis results from run {self.original_course_id}")
        return cached

    def _find_matching_job(self, task: str) -> dict | None:
        """Find cached job by task similarity (fuzzy match)."""
        # Exact match first (most deterministic), then fuzzy fallback.
        for job_data in self.cached_jobs.values():
            job_id = int(job_data["job_id"])
            if job_id in self.used_job_ids:
                continue
            if job_data["task"] == task:
                logger.info(f"Cache hit (exact match): {task[:80]}...")
                return job_data

        best_match = None
        best_ratio = 0.0

        for job_data in self.cached_jobs.values():
            job_id = int(job_data["job_id"])
            if job_id in self.used_job_ids:
                continue
            ratio = SequenceMatcher(None, task, job_data["task"]).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = job_data

        if best_ratio >= self.match_threshold:
            logger.info(f"Cache hit (similarity={best_ratio:.0%}): {task[:50]}...")
            return best_match

        logger.warning(f"Cache miss (best={best_ratio:.0%}): {task[:50]}...")
        return None

    async def __call__(
        self,
        task: str,
        model: str | None = None,
        wait: bool = False,
        timeout_seconds: float = 300.0,
        decision_mode: str = "heuristic",
    ) -> str:
        """Mock spawn_commis - returns cached results instead of spawning real commis."""
        self.stats.spawn_commis_calls += 1
        self.stats.tool_call_names.append("spawn_commis")

        matching_job = self._find_matching_job(task)

        if not matching_job:
            self.stats.cache_misses += 1
            return (
                f"[REPLAY MOCK] No cached result for task.\n"
                f"Task: {task[:200]}\n\n"
                f"In production, a commis would be spawned. Returning synthetic response."
            )

        # Mark the matched cached job as used to avoid reusing a single cached job
        # for multiple spawn_commis calls in the replay.
        self.used_job_ids.add(int(matching_job["job_id"]))
        self.stats.cache_hits += 1
        job_id = matching_job["job_id"]
        commis_id = matching_job["commis_id"]
        status = matching_job["status"]

        if not wait:
            # Fire-and-forget mode: return queued message
            return (
                f"[REPLAY MOCK] Commis job {job_id} (cached) queued.\n"
                f"Task: {task[:100]}\n"
                f"Original status: {status}\n\n"
                f"Use read_commis_result('{job_id}') to get results."
            )

        # Wait mode: return actual cached result
        if status == "success" and commis_id:
            try:
                result = self.artifact_store.get_commis_result(commis_id)
                return f"[REPLAY MOCK - cached from job {job_id}]\n\n{result}"
            except Exception as e:
                logger.error(f"Failed to read cached result for {commis_id}: {e}")
                return f"[REPLAY MOCK] Error reading cached result: {e}"
        elif status == "failed":
            error = matching_job.get("error", "Unknown error")
            return f"[REPLAY MOCK] Commis job {job_id} failed: {error}"
        else:
            return f"[REPLAY MOCK] Commis job {job_id} status: {status}"

    def sync_wrapper(
        self,
        task: str,
        model: str | None = None,
        wait: bool = False,
        timeout_seconds: float = 300.0,
        decision_mode: str = "heuristic",
    ) -> str:
        """Sync wrapper for the mock (matches spawn_commis signature)."""
        try:
            asyncio.get_running_loop()
            raise RuntimeError(
                "spawn_commis sync wrapper was called while an event loop is already running. "
                "This usually indicates the tool was executed synchronously in an async context."
            )
        except RuntimeError as e:
            if "no running event loop" not in str(e).lower():
                raise

        # Run the async version in a new event loop
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self(task, model, wait, timeout_seconds, decision_mode))
        finally:
            loop.close()


class ToolMocker:
    """Context manager that patches StructuredTool instances directly.

    This is the correct way to mock tools - by replacing the coroutine/func
    attributes on the actual StructuredTool instances that the LLM uses.

    The naive approach of patching the module-level function doesn't work because:
    1. StructuredTool instances are created at import time
    2. They capture a reference to the original coroutine in their attributes
    3. bind_tools() uses these StructuredTool instances, not the module functions
    """

    def __init__(self, tool_name: str, mock_async: Callable, mock_sync: Callable):
        """Initialize the tool mocker.

        Args:
            tool_name: Name of the tool to mock (e.g., "spawn_commis")
            mock_async: Async function to replace the tool's coroutine
            mock_sync: Sync function to replace the tool's func
        """
        self.tool_name = tool_name
        self.mock_async = mock_async
        self.mock_sync = mock_sync
        self.original_coroutine = None
        self.original_func = None
        self.tool = None

    def __enter__(self):
        """Patch the tool's coroutine and func attributes."""
        resolver = get_tool_resolver()
        self.tool = resolver.get_tool(self.tool_name)

        if self.tool is None:
            raise ValueError(f"Tool '{self.tool_name}' not found in registry")

        # Save originals
        self.original_coroutine = self.tool.coroutine
        self.original_func = self.tool.func

        # Replace with mocks
        self.tool.coroutine = self.mock_async
        self.tool.func = self.mock_sync

        logger.info(f"Patched tool '{self.tool_name}' with mock implementation")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Restore the original coroutine and func."""
        if self.tool is not None:
            self.tool.coroutine = self.original_coroutine
            self.tool.func = self.original_func
        logger.info(f"Restored original '{self.tool_name}' implementation")
        return False  # Don't suppress exceptions


SAFE_DEFAULT_TOOLS = {
    # Concierge/commis inspection (read-only)
    "list_commis",
    "read_commis_result",
    "read_commis_file",
    "grep_commis",
    "get_commis_metadata",
    "get_current_time",
    # Runner inspection (read-only)
    "runner_list",
    # Research (slow/$$ but read-only)
    "knowledge_search",
    "web_search",
    "web_fetch",
}


def utc_now_naive() -> datetime:
    """UTC 'naive' timestamp (matches existing DB convention for Course timestamps)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def list_recent_runs(db: Session, limit: int = 20) -> None:
    """Print recent Course rows to help find replay targets."""
    runs = db.query(Course).order_by(Course.id.desc()).limit(limit).all()

    print("\n" + "=" * 80)
    print(f"RECENT RUNS (showing {len(runs)})")
    print("=" * 80)
    print(f"\n{'Run ID':<10} {'Status':<12} {'Thread':<10} {'Fiche':<10} {'Duration':<10} {'Created At'}")
    print("-" * 80)

    for run in runs:
        status = run.status.value if hasattr(run.status, "value") else str(run.status)
        duration = f"{run.duration_ms}ms" if run.duration_ms else "-"
        created_at = run.created_at.isoformat() if run.created_at else "-"
        print(f"{run.id:<10} {status:<12} {run.thread_id:<10} {run.fiche_id:<10} {duration:<10} {created_at}")

    print("\nTip: pick a course_id and re-run with: uv run python scripts/replay_run.py <course_id> --dry-run")


def get_run_time_window(run: Course) -> tuple[datetime | None, datetime | None, bool]:
    """Return (start, end, valid) for message-window filtering."""
    run_start = normalize_datetime(run.started_at) or normalize_datetime(run.created_at)
    run_end = normalize_datetime(run.finished_at)
    time_window_valid = run_start is not None
    return run_start, run_end, time_window_valid


def find_task_message(db: Session, run: Course) -> tuple[ThreadMessage | None, bool]:
    """Find the user message representing the task for this run."""
    run_start, run_end, time_window_valid = get_run_time_window(run)

    query = db.query(ThreadMessage).filter(
        ThreadMessage.thread_id == run.thread_id,
        ThreadMessage.role == "user",
    )
    if run_start:
        query = query.filter(ThreadMessage.sent_at >= run_start)
    if run_end:
        query = query.filter(ThreadMessage.sent_at <= run_end)

    user_msgs = query.order_by(ThreadMessage.sent_at).all()
    if not user_msgs:
        return None, time_window_valid

    # If the time window is suspect, prefer the LAST user message (more likely correct in long-lived threads).
    return (user_msgs[0] if time_window_valid else user_msgs[-1]), time_window_valid


def print_header(original_run: Course):
    """Print header with original run info."""
    print("\n" + "=" * 80)
    print("GOLDEN RUN REPLAY HARNESS")
    print("=" * 80)
    status = original_run.status.value if hasattr(original_run.status, "value") else original_run.status
    print(f"\nOriginal Run: #{original_run.id} [{status}]")
    print(f"Started:      {original_run.started_at}")
    print(f"Duration:     {original_run.duration_ms}ms" if original_run.duration_ms else "Duration: N/A")
    print(f"Tokens:       {original_run.total_tokens}" if original_run.total_tokens else "Tokens: N/A")
    print("=" * 80)


def get_run_summary(db: Session, run: Course) -> dict:
    """Extract summary data from a run, scoped to run's time window.

    IMPORTANT: Messages are filtered to only those within the run's time window
    (started_at to finished_at) to avoid counting messages from other runs
    on the same long-lived concierge thread.

    Args:
        db: Database session
        run: The Course to summarize

    Returns:
        Dictionary with task, tool_calls, commis, result, duration_ms, time_window_valid
    """
    run_start, run_end, time_window_valid = get_run_time_window(run)
    if not time_window_valid:
        logger.warning(
            f"Run {run.id} has no started_at/created_at - time window filtering disabled. "
            "Message counts may include other runs on this thread."
        )

    # Query messages with time bounds at the SQL level to avoid
    # loading the entire thread history into memory
    query = db.query(ThreadMessage).filter(ThreadMessage.thread_id == run.thread_id)

    # Apply time bounds if available
    if run_start:
        query = query.filter(ThreadMessage.sent_at >= run_start)
    if run_end:
        query = query.filter(ThreadMessage.sent_at <= run_end)

    messages = query.order_by(ThreadMessage.sent_at).all()

    # Find user message for this run (the task)
    task_msg, _time_window_valid = find_task_message(db, run)
    task = task_msg.content if task_msg else "(no task found)"

    # Count tool calls from assistant messages within the time window
    tool_call_count = sum(len(m.tool_calls) for m in messages if m.tool_calls)

    # Get commis spawned by this specific run (uses run.id, not time window)
    commis = db.query(CommisJob).filter(CommisJob.concierge_course_id == run.id).all()

    # Get final result (last assistant message with content in the time window)
    assistant_msgs = [m for m in messages if m.role == "assistant" and m.content]
    final_result = assistant_msgs[-1].content if assistant_msgs else "(no result)"

    return {
        "task": task,
        "tool_calls": tool_call_count,
        "commis": len(commis),
        "commis_tasks": [w.task[:60] for w in commis],
        "result": final_result,
        "duration_ms": run.duration_ms,
        "time_window_valid": time_window_valid,
    }


def print_comparison(original: dict, replay: dict, stats: ReplayStats):
    """Print side-by-side comparison."""
    print("\n" + "=" * 80)
    print("COMPARISON")
    print("=" * 80)

    # Warn if time window was invalid
    if not original.get("time_window_valid", True):
        print("\n⚠️  WARNING: Run has no started_at - original stats may include messages from other runs")

    print(f"\n{'Metric':<25} {'Original':<20} {'Replay':<20} {'Delta'}")
    print("-" * 80)

    # Duration
    orig_dur = original["duration_ms"] or 0
    replay_dur = replay["duration_ms"] or 0
    delta_dur = replay_dur - orig_dur
    print(f"{'Duration':<25} {orig_dur}ms{'':<13} {replay_dur}ms{'':<13} {delta_dur:+}ms")

    # Tool calls
    print(f"{'Tool Calls (all)':<25} {original['tool_calls']:<20} {replay['tool_calls']:<20}")
    print(f"{'  spawn_commis (mocked)':<25} {original['commis']:<20} {stats.spawn_commis_calls:<20}")
    if stats.blocked_tool_calls:
        print(f"{'  blocked tools':<25} {'-':<20} {stats.blocked_tool_calls:<20}")

    # Commis
    print(f"{'Commis Spawned':<25} {original['commis']:<20} {stats.spawn_commis_calls:<20}")
    print(f"{'  Cache Hits':<25} {'-':<20} {stats.cache_hits:<20}")
    print(f"{'  Cache Misses':<25} {'-':<20} {stats.cache_misses:<20}")

    # Result similarity
    similarity = SequenceMatcher(None, original["result"], replay["result"]).ratio()
    print(f"\n{'Result Similarity':<25} {similarity:.0%}")

    if similarity < 0.5:
        print("\n⚠️  Low similarity - results differ significantly")
        print(f"\nOriginal result (first 300 chars):\n{original['result'][:300]}...")
        print(f"\nReplay result (first 300 chars):\n{replay['result'][:300]}...")

    print("\n" + "=" * 80)


def build_replay_thread(
    db: Session,
    *,
    original_run: Course,
    replay_fiche,
    task_message: ThreadMessage,
    max_context_messages: int | None,
) -> tuple[Thread, int]:
    """Create an isolated replay thread with a snapshot of the original thread context."""
    replay_thread = crud.create_thread(
        db=db,
        fiche_id=replay_fiche.id,
        title=f"[REPLAY] original_run={original_run.id}",
        active=False,
        fiche_state={
            "replay": {
                "original_course_id": original_run.id,
                "original_thread_id": original_run.thread_id,
                "created_at": utc_now_naive().isoformat(),
            }
        },
        memory_strategy="buffer",
        thread_type=ThreadType.MANUAL.value,
    )

    # Copy non-system messages from the original thread up to (but excluding) the task message.
    # System messages in DB are intentionally excluded from LLM input (FicheRunner injects fresh system prompt).
    query = (
        db.query(ThreadMessage)
        .filter(
            ThreadMessage.thread_id == original_run.thread_id,
            ThreadMessage.sent_at < task_message.sent_at,
            ThreadMessage.role != "system",
        )
        .order_by(ThreadMessage.sent_at)
    )
    context_rows = query.all()

    if max_context_messages is not None and max_context_messages > 0 and len(context_rows) > max_context_messages:
        context_rows = context_rows[-max_context_messages:]

    last_assistant_id: int | None = None
    for row in context_rows:
        kwargs = {
            "role": row.role,
            "content": row.content,
            "tool_calls": copy.deepcopy(row.tool_calls) if row.tool_calls is not None else None,
            "tool_call_id": row.tool_call_id,
            "name": row.name,
            "sent_at": row.sent_at,
            "processed": True,
            "message_metadata": copy.deepcopy(row.message_metadata) if row.message_metadata is not None else None,
        }
        if row.role == "tool":
            kwargs["parent_id"] = last_assistant_id

        created = crud.create_thread_message(db=db, thread_id=replay_thread.id, commit=False, **kwargs)
        if created.role == "assistant":
            last_assistant_id = created.id

    db.commit()
    return replay_thread, len(context_rows)


def make_blocked_tool(tool_name: str, stats: ReplayStats) -> tuple[Callable, Callable]:
    """Create a blocked tool implementation (async + sync) for safety."""

    async def blocked_async(**_kwargs) -> str:
        stats.blocked_tool_calls += 1
        stats.blocked_calls_by_tool[tool_name] = stats.blocked_calls_by_tool.get(tool_name, 0) + 1
        stats.tool_call_names.append(tool_name)
        return (
            f"[REPLAY BLOCKED] Tool '{tool_name}' was called during replay, but it's blocked by default.\n\n"
            f"Re-run with --allow-tool {tool_name} (or --allow-all-tools) to execute it for real."
        )

    def blocked_sync(**_kwargs) -> str:
        stats.blocked_tool_calls += 1
        stats.blocked_calls_by_tool[tool_name] = stats.blocked_calls_by_tool.get(tool_name, 0) + 1
        stats.tool_call_names.append(tool_name)
        return (
            f"[REPLAY BLOCKED] Tool '{tool_name}' was called during replay, but it's blocked by default.\n\n"
            f"Re-run with --allow-tool {tool_name} (or --allow-all-tools) to execute it for real."
        )

    return blocked_async, blocked_sync


async def replay_run(
    db: Session,
    course_id: int,
    *,
    dry_run: bool = False,
    match_threshold: float = 0.7,
    max_context_messages: int | None = None,
    allow_all_tools: bool = False,
    allow_tools: list[str] | None = None,
    cleanup: bool = False,
):
    """Replay a concierge run with mocked tools."""
    # Load original run
    original_run = db.query(Course).filter(Course.id == course_id).first()
    if not original_run:
        print(f"❌ Run {course_id} not found")
        return

    print_header(original_run)

    # Get original summary
    original_summary = get_run_summary(db, original_run)
    print(f"\nTask: {original_summary['task'][:100]}...")
    print(f"Original tool calls: {original_summary['tool_calls']}")
    print(f"Original commis: {original_summary['commis']}")

    task_message, time_window_valid = find_task_message(db, original_run)
    if task_message is None:
        print("\n❌ Could not find task message for this run (no user messages in window)")
        return

    if not time_window_valid:
        print("\n⚠️  WARNING: Run has no started_at/created_at; time window may include other runs on this thread")

    # Always refresh concierge fiche (pulls latest prompt + tool allowlist from templates/user context)
    concierge_service = ConciergeService(db)
    replay_fiche = concierge_service.get_or_create_concierge_fiche(original_run.fiche.owner_id)

    allowed_tool_names = list(getattr(replay_fiche, "allowed_tools", None) or [])
    allow_tools_set = set(allow_tools or [])

    blocked_tools: list[str] = []
    if not allow_all_tools:
        for tool_name in allowed_tool_names:
            if tool_name == "spawn_commis":
                continue
            if tool_name in SAFE_DEFAULT_TOOLS:
                continue
            if tool_name in allow_tools_set:
                continue
            blocked_tools.append(tool_name)

    if dry_run:
        # Show snapshot context size without copying
        context_count = (
            db.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == original_run.thread_id,
                ThreadMessage.sent_at < task_message.sent_at,
                ThreadMessage.role != "system",
            )
            .count()
        )
        if max_context_messages is not None and max_context_messages > 0:
            context_note = (
                f" (tail {max_context_messages} of {context_count})" if context_count > max_context_messages else f" ({context_count})"
            )
        else:
            context_note = f" ({context_count})"

        print("\n[DRY RUN] Would replay with mocked spawn_commis in an isolated replay thread")
        print(f"Context messages to copy (non-system):{context_note}")
        print(f"Tool policy: {'ALLOW ALL' if allow_all_tools else 'SAFE DEFAULT'}")
        if blocked_tools:
            print("Blocked tools:")
            for name in blocked_tools:
                print(f"  - {name}")
        else:
            print("Blocked tools: (none)")

        print("Cached commis tasks:")
        for task in original_summary["commis_tasks"]:
            print(f"  - {task}...")
        return

    # Create isolated replay thread and run record
    replay_thread, copied_count = build_replay_thread(
        db,
        original_run=original_run,
        replay_fiche=replay_fiche,
        task_message=task_message,
        max_context_messages=max_context_messages,
    )

    replay_run_row = Course(
        fiche_id=replay_fiche.id,
        thread_id=replay_thread.id,
        status=CourseStatus.RUNNING,
        trigger=CourseTrigger.API,
        correlation_id=f"replay:{original_run.id}:{utc_now_naive().isoformat()}",
        started_at=utc_now_naive(),
    )
    db.add(replay_run_row)
    db.commit()
    db.refresh(replay_run_row)

    # Add task as an unprocessed user message (FicheRunner will pick it up)
    crud.create_thread_message(
        db=db,
        thread_id=replay_thread.id,
        role="user",
        content=original_summary["task"],
        processed=False,
    )

    print("\n--- STARTING REPLAY (isolated thread + mocked spawn_commis) ---\n")
    print(f"Replay run:   #{replay_run_row.id} (thread {replay_thread.id})")
    print(f"Context size: {copied_count} message(s) copied from original thread history")
    if blocked_tools:
        print(f"Tool policy:  SAFE DEFAULT (blocking {len(blocked_tools)} tool(s): {', '.join(blocked_tools)})")
    else:
        print(f"Tool policy:  {'ALLOW ALL' if allow_all_tools else 'SAFE DEFAULT'}")

    stats = ReplayStats()
    mock_spawn = MockedSpawnCommis(db, course_id, stats, match_threshold=match_threshold)

    start_time = datetime.now(timezone.utc)

    with ExitStack() as stack:
        # Patch spawn_commis tool directly on the StructuredTool instance
        stack.enter_context(ToolMocker("spawn_commis", mock_spawn, mock_spawn.sync_wrapper))

        # Optionally block side-effect tools for safety
        for tool_name in blocked_tools:
            blocked_async, blocked_sync = make_blocked_tool(tool_name, stats)
            stack.enter_context(ToolMocker(tool_name, blocked_async, blocked_sync))

        try:
            runner = FicheRunner(replay_fiche)
            created_messages = await runner.run_thread(db, replay_thread)

            # Extract final result (last assistant message)
            result_text = None
            for msg in reversed(created_messages):
                if msg.role == "assistant" and msg.content:
                    result_text = msg.content
                    break

            end_time = datetime.now(timezone.utc)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)

            replay_run_row.status = CourseStatus.SUCCESS
            replay_run_row.finished_at = end_time.replace(tzinfo=None)
            replay_run_row.duration_ms = duration_ms
            if runner.usage_total_tokens:
                replay_run_row.total_tokens = runner.usage_total_tokens
            replay_run_row.summary = (result_text or "")[:500] if result_text else None
            db.commit()

            print("\n✅ Replay complete: success")
            print(f"   Replay course_id: {replay_run_row.id}")

            replay_summary = get_run_summary(db, replay_run_row)
            replay_summary["result"] = result_text or "(no result)"
            replay_summary["duration_ms"] = duration_ms

            print_comparison(original_summary, replay_summary, stats)

        except Exception as e:
            end_time = datetime.now(timezone.utc)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)

            replay_run_row.status = CourseStatus.FAILED
            replay_run_row.finished_at = end_time.replace(tzinfo=None)
            replay_run_row.duration_ms = duration_ms
            replay_run_row.error = str(e)
            db.commit()

            logger.exception(f"Replay failed: {e}")
            print(f"\n❌ Replay failed: {e}")

        finally:
            if cleanup:
                # Best-effort cleanup to avoid DB pollution (only deletes the replay artifacts).
                try:
                    # Remove run first (thread FK constraint).
                    db.delete(replay_run_row)
                    db.commit()
                except Exception:
                    db.rollback()
                try:
                    crud.delete_thread(db, replay_thread.id)
                except Exception:
                    db.rollback()


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Replay a concierge run with mocked tools to test prompt changes")
    parser.add_argument("course_id", type=int, nargs="?", help="Run ID to replay")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be replayed without running",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete the replay thread + run record after finishing (best-effort)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--match-threshold",
        type=float,
        default=0.7,
        help="Fuzzy match threshold for cached commis task selection (default: 0.7)",
    )
    parser.add_argument(
        "--max-context-messages",
        type=int,
        default=None,
        help="Copy only the last N (non-system) messages from the original thread as context",
    )
    parser.add_argument(
        "--allow-tool",
        action="append",
        default=[],
        help="Allow a tool that is blocked by default (repeatable)",
    )
    parser.add_argument(
        "--allow-all-tools",
        action="store_true",
        help="Allow all concierge tools (dangerous: can send email / make network calls)",
    )
    parser.add_argument(
        "--list-recent",
        nargs="?",
        const=20,
        type=int,
        help="List the most recent runs and exit (default: 20)",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Get database session (friendly errors when Postgres isn't up)
    db = None
    db_gen = get_db()
    try:
        db = next(db_gen)
    except Exception as e:
        print("\n❌ Database connection failed.")
        print("Local dev: start the stack with `make dev-bg` (or `make dev`) so Postgres is running.")
        print("Also verify your `.env` / `DATABASE_URL` points at the correct DB.")
        print(f"\nUnderlying error: {e}")
        return

    try:
        if args.list_recent is not None:
            list_recent_runs(db, limit=args.list_recent)
            return

        if args.course_id is None:
            parser.error("course_id is required unless --list-recent is used")

        asyncio.run(
            replay_run(
                db,
                args.course_id,
                dry_run=args.dry_run,
                match_threshold=args.match_threshold,
                max_context_messages=args.max_context_messages,
                allow_all_tools=args.allow_all_tools,
                allow_tools=args.allow_tool,
                cleanup=args.cleanup,
            )
        )
    except OperationalError as e:
        print("\n❌ Database operation failed.")
        print("Local dev: start the stack with `make dev-bg` (or `make dev`) so Postgres is running.")
        print("Also verify your `.env` / `DATABASE_URL` points at the correct DB.")
        print(f"\nUnderlying error: {getattr(e, 'orig', e)}")
    except KeyboardInterrupt:
        print("\n\n⚠️ Interrupted by user")
    finally:
        if db is not None:
            db.close()
        try:
            db_gen.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
