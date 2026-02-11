"""LangGraph-free ReAct engine for oikos fiches.

Pure async ReAct loop: messages in, messages + usage out. No checkpointer,
no interrupt(). spawn_commis uses two-phase commit in parallel execution
(returns ToolMessages + interrupt_value); single-tool calls raise FicheInterrupted.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING

from zerg.context import get_commis_context
from zerg.managers.fiche_runner import FicheInterrupted
from zerg.services.openai_client import OpenAIChat
from zerg.tools.result_utils import is_critical_tool_error
from zerg.types.messages import AIMessage
from zerg.types.messages import BaseMessage
from zerg.types.messages import SystemMessage
from zerg.types.messages import ToolMessage

if TYPE_CHECKING:
    from zerg.types.tools import Tool as BaseTool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Usage tracking (context-var accumulator)
# ---------------------------------------------------------------------------

_llm_usage_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar("llm_usage", default=None)
MAX_REACT_ITERATIONS = 50


def _empty_usage() -> dict:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "reasoning_tokens": 0}


def reset_llm_usage() -> None:
    """Reset accumulated LLM usage. Call before starting a new run."""
    _llm_usage_var.set(None)


def get_llm_usage() -> dict:
    usage = _llm_usage_var.get()
    return usage if usage is not None else {}


def _accumulate_llm_usage(usage: dict) -> None:
    current = _llm_usage_var.get() or _empty_usage()
    current["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
    current["completion_tokens"] += usage.get("completion_tokens", 0) or 0
    current["total_tokens"] += usage.get("total_tokens", 0) or 0
    details = usage.get("completion_tokens_details") or {}
    current["reasoning_tokens"] += details.get("reasoning_tokens", 0) or 0
    _llm_usage_var.set(current)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class OikosResult:
    """Result from oikos ReAct loop execution."""

    messages: list[BaseMessage]
    usage: dict = field(default_factory=dict)
    interrupted: bool = False
    interrupt_value: dict | None = None


# ---------------------------------------------------------------------------
# LLM Factory
# ---------------------------------------------------------------------------


def _make_llm(
    model: str,
    tools: list[BaseTool],
    *,
    reasoning_effort: str = "none",
    tool_choice: dict | str | bool | None = None,
):
    """Create a tool-bound LLM instance."""
    from zerg.config import get_settings
    from zerg.testing.test_models import is_test_model
    from zerg.testing.test_models import warn_if_test_model

    if is_test_model(model):
        warn_if_test_model(model)
        if model == "gpt-mock":
            from zerg.testing.mock_llm import MockChatLLM

            llm = MockChatLLM()
        elif model == "gpt-scripted":
            from zerg.testing.scripted_llm import ScriptedChatLLM

            llm = ScriptedChatLLM(sequences=[])
        else:
            raise ValueError(f"Unknown test model: {model}")
        try:
            return llm.bind_tools(tools, tool_choice=tool_choice)
        except TypeError:
            return llm.bind_tools(tools)

    from zerg.models_config import ModelProvider
    from zerg.models_config import get_all_models
    from zerg.models_config import get_model_by_id

    model_config = get_model_by_id(model)
    if not model_config:
        available = [m.id for m in get_all_models()]
        raise ValueError(f"Unknown model: {model}. Available: {available}")

    settings = get_settings()
    provider = model_config.provider

    kwargs: dict = {
        "model": model,
        "streaming": settings.llm_token_stream,
        "api_key": settings.groq_api_key if provider == ModelProvider.GROQ else settings.openai_api_key,
    }
    if provider == ModelProvider.GROQ:
        kwargs["base_url"] = model_config.base_url
        if not kwargs["api_key"]:
            raise ValueError(f"GROQ_API_KEY not configured but Groq model '{model}' selected")

    capabilities = model_config.capabilities or {}
    if capabilities.get("reasoning", False):
        effort = reasoning_effort
        if effort == "none" and not capabilities.get("reasoningNone", False):
            effort = "low"
        kwargs["reasoning_effort"] = effort

    llm = OpenAIChat(**kwargs)
    if tool_choice is None:
        return llm.bind_tools(tools)
    try:
        return llm.bind_tools(tools, tool_choice=tool_choice)
    except TypeError:
        return llm.bind_tools(tools)


# ---------------------------------------------------------------------------
# Heartbeat emission during LLM calls
# ---------------------------------------------------------------------------


async def _emit_heartbeats(cancelled: asyncio.Event, run_id: int | None, owner_id: int | None, phase: str) -> None:
    """Emit heartbeats every 10s during LLM call."""
    from zerg.events import EventType
    from zerg.events import event_bus

    try:
        while not cancelled.is_set():
            await asyncio.sleep(10)
            if cancelled.is_set():
                break
            if run_id is not None:
                await event_bus.publish(
                    EventType.OIKOS_HEARTBEAT,
                    {
                        "event_type": EventType.OIKOS_HEARTBEAT,
                        "run_id": run_id,
                        "owner_id": owner_id,
                        "activity": "llm_reasoning",
                        "phase": phase,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )
    except (asyncio.CancelledError, Exception):
        pass


# ---------------------------------------------------------------------------
# LLM Call with streaming, audit logging, heartbeats
# ---------------------------------------------------------------------------


async def _call_llm(
    messages: list[BaseMessage],
    llm_with_tools,
    *,
    phase: str,
    run_id: int | None,
    owner_id: int | None,
    model: str,
    trace_id: str | None = None,
    enable_token_stream: bool = False,
) -> AIMessage:
    """Call LLM with heartbeats, audit logging, and usage tracking."""
    from zerg.services.llm_audit import audit_logger

    start = datetime.now(timezone.utc)
    heartbeat_cancelled = asyncio.Event()
    heartbeat_task = asyncio.create_task(_emit_heartbeats(heartbeat_cancelled, run_id, owner_id, phase))
    audit_id = None

    try:
        audit_id = await audit_logger.log_request(
            run_id=run_id,
            commis_id=None,
            owner_id=owner_id,
            trace_id=trace_id,
            phase=phase,
            model=model,
            messages=messages,
        )
        if enable_token_stream:
            from zerg.callbacks.token_stream import WsTokenCallback

            result = await llm_with_tools.ainvoke(messages, config={"callbacks": [WsTokenCallback()]})
        else:
            result = await llm_with_tools.ainvoke(messages)
    except Exception as e:
        duration_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
        if audit_id is not None:
            try:
                await audit_logger.log_response(
                    correlation_id=audit_id,
                    content=None,
                    tool_calls=None,
                    input_tokens=None,
                    output_tokens=None,
                    reasoning_tokens=None,
                    duration_ms=duration_ms,
                    error=str(e),
                )
            except Exception:
                pass
        raise
    finally:
        heartbeat_cancelled.set()
        try:
            await asyncio.wait_for(heartbeat_task, timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    duration_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)

    # Audit log response
    try:
        usage_meta = getattr(result, "usage_metadata", {}) or {} if isinstance(result, AIMessage) else {}
        await audit_logger.log_response(
            correlation_id=audit_id,
            content=result.content if isinstance(result, AIMessage) else None,
            tool_calls=result.tool_calls if isinstance(result, AIMessage) else None,
            input_tokens=usage_meta.get("input_tokens"),
            output_tokens=usage_meta.get("output_tokens"),
            reasoning_tokens=(usage_meta.get("output_token_details", {}).get("reasoning") if usage_meta else None),
            duration_ms=duration_ms,
        )
    except Exception:
        pass

    # Accumulate usage
    if isinstance(result, AIMessage):
        usage_meta = getattr(result, "usage_metadata", None)
        if usage_meta:
            _accumulate_llm_usage(
                {
                    "prompt_tokens": usage_meta.get("input_tokens", 0),
                    "completion_tokens": usage_meta.get("output_tokens", 0),
                    "total_tokens": usage_meta.get("total_tokens", 0),
                    "completion_tokens_details": {"reasoning_tokens": usage_meta.get("output_token_details", {}).get("reasoning", 0)},
                }
            )

            # Log prompt cache metrics
            input_tokens = usage_meta.get("input_tokens", 0)
            cache_read = usage_meta.get("cache_read_input_tokens", 0)
            if input_tokens > 0:
                cache_pct = round(cache_read / input_tokens * 100, 1) if cache_read else 0.0
                logger.info(
                    "LLM cache metrics: phase=%s model=%s input=%d cached=%d ratio=%.1f%% duration=%dms",
                    phase,
                    model,
                    input_tokens,
                    cache_read,
                    cache_pct,
                    duration_ms,
                )

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_text_content(msg: AIMessage) -> str:
    """Extract text content from an AIMessage (handles str or list-of-parts)."""
    content = msg.content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return str(content or "")


def _json_default(obj):
    """JSON serializer for datetime objects in tool results."""
    from datetime import date as date_type

    if isinstance(obj, (datetime, date_type)):
        return obj.isoformat()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


# ---------------------------------------------------------------------------
# Tool execution with event emission
# ---------------------------------------------------------------------------


async def _execute_tool(
    tool_call: dict,
    tools_by_name: dict[str, BaseTool],
    *,
    run_id: int | None,
    owner_id: int | None,
    tool_getter: callable | None = None,
) -> ToolMessage:
    """Execute a single tool call with event emission.

    For spawn_commis variants, raises FicheInterrupted for queued jobs.
    """
    from zerg.events import get_emitter
    from zerg.tools.result_utils import check_tool_error
    from zerg.tools.result_utils import redact_sensitive_args
    from zerg.tools.result_utils import safe_preview

    tool_name = tool_call.get("name", "unknown_tool")
    tool_args = tool_call.get("args", {})
    tool_call_id = tool_call.get("id", "")
    emitter = get_emitter()
    safe_args = redact_sensitive_args(tool_args)

    if emitter:
        await emitter.emit_tool_started(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_args_preview=safe_preview(str(safe_args)),
            tool_args=safe_args,
        )

    start_time = datetime.now(timezone.utc)
    result_content = None

    # Resolve tool
    tool_to_call = tool_getter(tool_name) if tool_getter else tools_by_name.get(tool_name)
    if not tool_to_call:
        result_content = f"Error: Tool '{tool_name}' not found."
        logger.error(result_content)
    else:
        try:
            # Spawn-type tools: interrupt handling for two-phase commit
            if tool_name in ("spawn_commis", "spawn_workspace_commis"):
                job_result = await _call_spawn_tool(tool_to_call, tool_args, tool_call_id)
                if isinstance(job_result, dict):
                    job_id = job_result.get("job_id")
                    if job_result.get("status") == "queued" and job_id is not None:
                        duration_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)
                        if emitter:
                            await emitter.emit_tool_completed(
                                tool_name=tool_name,
                                tool_call_id=tool_call_id,
                                duration_ms=duration_ms,
                                result_preview=f"Commis job {job_id} spawned",
                                result=str(job_result),
                            )
                        raise FicheInterrupted(
                            {
                                "type": "commis_pending",
                                "job_id": job_id,
                                "task": tool_args.get("task", "")[:100],
                                "model": tool_args.get("model"),
                                "tool_call_id": tool_call_id,
                            }
                        )
                    result_content = json.dumps(job_result)
                else:
                    result_content = str(job_result)

            elif tool_name == "wait_for_commis":
                from zerg.tools.builtin.oikos_tools import wait_for_commis_async

                observation = await wait_for_commis_async(job_id=tool_args.get("job_id", ""), _tool_call_id=tool_call_id)
                result_content = json.dumps(observation, default=_json_default) if isinstance(observation, dict) else str(observation)

            elif getattr(tool_to_call, "coroutine", None):
                observation = await tool_to_call.ainvoke(tool_args)
                result_content = json.dumps(observation, default=_json_default) if isinstance(observation, dict) else str(observation)
            else:
                observation = await asyncio.to_thread(tool_to_call.invoke, tool_args)
                result_content = json.dumps(observation, default=_json_default) if isinstance(observation, dict) else str(observation)

        except FicheInterrupted:
            raise
        except Exception as exc:
            result_content = f"<tool-error> {exc}"
            logger.exception("Error executing tool %s", tool_name)

    duration_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)
    if result_content is None:
        result_content = "(No result)"

    raw_result = str(result_content)
    is_error, error_msg = check_tool_error(raw_result)

    # Critical error detection (marks commis context + emitter for fail-fast)
    if is_error and is_critical_tool_error(raw_result, error_msg, tool_name=tool_name):
        ctx = get_commis_context()
        if ctx:
            ctx.mark_critical_error(error_msg or raw_result)
        if emitter:
            emitter.mark_critical_error(error_msg or raw_result)
        logger.warning(f"Critical tool error in {tool_name}: {error_msg or raw_result}")

    # Tool result truncation: large outputs stored as artifacts
    result_content = _maybe_truncate_result(raw_result, tool_name, run_id, owner_id, tool_call_id)

    # Emit completion event
    if emitter:
        if is_error:
            await emitter.emit_tool_failed(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                duration_ms=duration_ms,
                error=safe_preview(error_msg or result_content, 500),
            )
        else:
            await emitter.emit_tool_completed(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                duration_ms=duration_ms,
                result_preview=safe_preview(result_content),
                result=result_content,
            )

    return ToolMessage(content=result_content, tool_call_id=tool_call_id, name=tool_name)


async def _call_spawn_tool(tool: BaseTool, tool_args: dict, tool_call_id: str):
    """Invoke a spawn_commis variant and return structured result."""
    args = dict(tool_args)
    args["_tool_call_id"] = tool_call_id
    args["_return_structured"] = True
    if getattr(tool, "coroutine", None):
        return await tool.ainvoke(args)
    return await asyncio.to_thread(tool.invoke, args)


def _maybe_truncate_result(
    raw: str,
    tool_name: str,
    run_id: int | None,
    owner_id: int | None,
    tool_call_id: str,
) -> str:
    """Store large tool outputs out-of-band and return truncated preview."""
    from zerg.config import get_settings
    from zerg.services.tool_output_store import ToolOutputStore

    settings = get_settings()
    max_chars = max(0, int(settings.oikos_tool_output_max_chars or 0))
    if max_chars <= 0 or len(raw) <= max_chars or tool_name == "get_tool_output":
        return raw

    preview_chars = max(0, int(settings.oikos_tool_output_preview_chars or 0))
    preview_chars = min(preview_chars, max_chars) if preview_chars > 0 else min(200, max_chars)
    preview = raw[:preview_chars]

    if owner_id is None:
        return f"(Tool output truncated; exceeded {max_chars} characters.)\nFull output was not stored (no owner_id).\nPreview:\n{preview}"

    try:
        store = ToolOutputStore()
        artifact_id = store.save_output(owner_id=owner_id, tool_name=tool_name, content=raw, run_id=run_id, tool_call_id=tool_call_id)
        size_bytes = len(raw.encode("utf-8"))
        return (
            f"[TOOL_OUTPUT:artifact_id={artifact_id},tool={tool_name},bytes={size_bytes}]\n"
            f"Tool output exceeded {max_chars} characters and was stored out of band.\n"
            f"Preview (first {preview_chars} chars):\n{preview}\n\n"
            "Use get_tool_output(artifact_id) to fetch the full output."
        )
    except Exception:
        logger.exception("Failed to store tool output for %s", tool_name)
        return (
            f"(Tool output truncated; exceeded {max_chars} characters.)\nFull output was not stored (storage failed).\nPreview:\n{preview}"
        )


# ---------------------------------------------------------------------------
# Parallel Tool Execution
# ---------------------------------------------------------------------------


async def _execute_tools_parallel(
    tool_calls: list[dict],
    tools_by_name: dict[str, BaseTool],
    *,
    run_id: int | None,
    owner_id: int | None,
    tool_getter: callable | None = None,
) -> tuple[list[ToolMessage], dict | None]:
    """Execute tools in parallel with two-phase commit for spawn_commis.

    Non-spawn tools run concurrently via asyncio.gather().
    Spawn_commis calls create jobs with status='created' (not 'queued').
    Returns (tool_results, interrupt_value) where interrupt_value triggers barrier creation.
    """
    spawn_calls = [tc for tc in tool_calls if tc.get("name") == "spawn_commis"]
    other_calls = [tc for tc in tool_calls if tc.get("name") != "spawn_commis"]
    tool_results: list[ToolMessage] = []

    # Phase 1: Execute non-spawn tools concurrently
    if other_calls:

        async def _exec(tc: dict) -> ToolMessage:
            try:
                return await _execute_tool(tc, tools_by_name, run_id=run_id, owner_id=owner_id, tool_getter=tool_getter)
            except FicheInterrupted:
                raise
            except Exception as exc:
                logger.exception(f"Parallel tool error: {tc.get('name')}")
                return ToolMessage(
                    content=f"<tool-error>{exc}</tool-error>",
                    tool_call_id=tc.get("id", ""),
                    name=tc.get("name", "unknown"),
                )

        results = await asyncio.gather(*[_exec(tc) for tc in other_calls], return_exceptions=True)
        for tc, result in zip(other_calls, results):
            if isinstance(result, FicheInterrupted):
                raise result
            elif isinstance(result, Exception):
                tool_results.append(
                    ToolMessage(
                        content=f"<tool-error>{result}</tool-error>",
                        tool_call_id=tc.get("id", ""),
                        name=tc.get("name", "unknown"),
                    )
                )
            else:
                tool_results.append(result)

    # Phase 2: Two-phase commit for spawn_commis
    if spawn_calls:
        results, interrupt = await _handle_spawn_calls(spawn_calls, run_id, owner_id)
        tool_results.extend(results)
        if interrupt:
            return tool_results, interrupt

    return tool_results, None


async def _handle_spawn_calls(
    spawn_calls: list[dict],
    run_id: int | None,
    owner_id: int | None,
) -> tuple[list[ToolMessage], dict | None]:
    """Handle spawn_commis calls with two-phase commit pattern.

    Jobs created as 'created' (not 'queued'). Caller (oikos_service) creates
    CommisBarrier and flips to 'queued' to prevent the fast-commis race.
    """
    import uuid as uuid_module

    from zerg.connectors.context import get_credential_resolver
    from zerg.events.oikos_emitter import OikosEmitter
    from zerg.models.models import CommisJob
    from zerg.services.oikos_context import get_oikos_context

    resolver = get_credential_resolver()
    ctx = get_oikos_context()
    tool_results: list[ToolMessage] = []

    emitter = None
    if ctx:
        emitter = OikosEmitter(run_id=ctx.run_id, owner_id=ctx.owner_id, message_id=ctx.message_id, trace_id=ctx.trace_id)

    if not resolver:
        for tc in spawn_calls:
            tool_results.append(
                ToolMessage(
                    content="<tool-error>Cannot spawn commis - no credential context</tool-error>",
                    tool_call_id=tc.get("id", ""),
                    name="spawn_commis",
                )
            )
        return tool_results, None

    db = resolver.db
    oikos_run_id = ctx.run_id if ctx else None
    trace_id = ctx.trace_id if ctx else None
    commis_model = (ctx.model if ctx else None) or "gpt-5-mini"
    commis_reasoning_effort = (ctx.reasoning_effort if ctx else None) or "none"

    created_jobs: list[dict] = []

    for tc in spawn_calls:
        tool_args = tc.get("args", {})
        task = tool_args.get("task", "")
        model_override = tool_args.get("model")
        git_repo = tool_args.get("git_repo")
        resume_session_id = tool_args.get("resume_session_id")
        tool_call_id = tc.get("id", "")
        start = time.time()

        # Build workspace config
        job_config: dict | None = None
        if git_repo:
            job_config = {"execution_mode": "workspace", "git_repo": git_repo}
            if resume_session_id:
                job_config["resume_session_id"] = resume_session_id
        elif resume_session_id:
            job_config = {"resume_session_id": resume_session_id}

        if emitter:
            await emitter.emit_tool_started(
                tool_name="spawn_commis",
                tool_call_id=tool_call_id,
                tool_args_preview=task[:100],
                tool_args={"task": task, "model": model_override},
            )

        try:
            # Idempotency: check for existing job
            existing_job = None
            if tool_call_id and oikos_run_id:
                existing_job = (
                    db.query(CommisJob)
                    .filter(
                        CommisJob.oikos_run_id == oikos_run_id,
                        CommisJob.tool_call_id == tool_call_id,
                    )
                    .first()
                )

            if existing_job and existing_job.status == "success":
                from zerg.services.commis_artifact_store import CommisArtifactStore

                artifact_store = CommisArtifactStore()
                try:
                    metadata = artifact_store.get_commis_metadata(existing_job.commis_id)
                    result = metadata.get("summary") or artifact_store.get_commis_result(existing_job.commis_id)
                    tool_results.append(
                        ToolMessage(
                            content=f"Commis job {existing_job.id} completed:\n\n{result}",
                            tool_call_id=tool_call_id,
                            name="spawn_commis",
                        )
                    )
                    if emitter:
                        await emitter.emit_tool_completed(
                            tool_name="spawn_commis",
                            tool_call_id=tool_call_id,
                            duration_ms=int((time.time() - start) * 1000),
                            result_preview=f"Cached result for job {existing_job.id}",
                            result={"job_id": existing_job.id, "status": "success", "cached": True},
                        )
                    continue
                except FileNotFoundError:
                    pass

            if existing_job and existing_job.status in ("queued", "running", "created"):
                created_jobs.append({"job": existing_job, "tool_call_id": tool_call_id, "task": task[:100]})
                if emitter:
                    await emitter.emit_tool_completed(
                        tool_name="spawn_commis",
                        tool_call_id=tool_call_id,
                        duration_ms=int((time.time() - start) * 1000),
                        result_preview=f"Reusing existing job {existing_job.id}",
                        result={"job_id": existing_job.id, "status": existing_job.status, "task": task[:100]},
                    )
                continue

            # Create new job with status='created' (TWO-PHASE COMMIT)
            commis_job = CommisJob(
                owner_id=resolver.owner_id,
                oikos_run_id=oikos_run_id,
                tool_call_id=tool_call_id,
                trace_id=uuid_module.UUID(trace_id) if trace_id else None,
                task=task,
                model=model_override or commis_model,
                reasoning_effort=commis_reasoning_effort,
                status="created",
                config=job_config,
            )
            db.add(commis_job)
            db.commit()
            db.refresh(commis_job)

            logger.info(
                f"[PARALLEL-SPAWN] Created commis job {commis_job.id} status='created'" + (f", config={job_config}" if job_config else "")
            )

            created_jobs.append({"job": commis_job, "tool_call_id": tool_call_id, "task": task[:100]})

            if emitter:
                await emitter.emit_tool_completed(
                    tool_name="spawn_commis",
                    tool_call_id=tool_call_id,
                    duration_ms=int((time.time() - start) * 1000),
                    result_preview=f"Created job {commis_job.id}",
                    result={"job_id": commis_job.id, "status": "created", "task": task[:100]},
                )

        except Exception as exc:
            logger.exception(f"Error creating spawn_commis job: {task[:50]}")
            db.rollback()
            if emitter:
                await emitter.emit_tool_failed(
                    tool_name="spawn_commis",
                    tool_call_id=tool_call_id,
                    duration_ms=int((time.time() - start) * 1000),
                    error=str(exc),
                )
            tool_results.append(
                ToolMessage(
                    content=f"<tool-error>Failed to spawn commis: {exc}</tool-error>",
                    tool_call_id=tool_call_id,
                    name="spawn_commis",
                )
            )

    if created_jobs:
        for job_info in created_jobs:
            job = job_info["job"]
            tool_results.append(
                ToolMessage(
                    content=f"Commis job {job.id} spawned successfully. Working on: {job_info.get('task', '')}\n\nWaiting for commis to complete...",
                    tool_call_id=job_info["tool_call_id"],
                    name="spawn_commis",
                )
            )
        interrupt_value = {
            "type": "commiss_pending",
            "job_ids": [j["job"].id for j in created_jobs],
            "created_jobs": created_jobs,
        }
        logger.info(f"[PARALLEL-SPAWN] Interrupt with {len(created_jobs)} jobs for barrier creation")
        return tool_results, interrupt_value

    return tool_results, None


# ---------------------------------------------------------------------------
# Main ReAct Loop
# ---------------------------------------------------------------------------


async def run_oikos_loop(
    messages: list[BaseMessage],
    fiche_row=None,
    tools: list[BaseTool] | None = None,
    *,
    run_id: int | None = None,
    owner_id: int | None = None,
    trace_id: str | None = None,
    enable_token_stream: bool = False,
    lazy_loading: bool = False,
) -> OikosResult:
    """Run the oikos ReAct loop until completion or interrupt."""
    if tools is None:
        tools = []

    # Set up tool binding (lazy or eager)
    if lazy_loading:
        from zerg.tools import get_registry
        from zerg.tools.lazy_binder import LazyToolBinder
        from zerg.tools.tool_search import build_catalog
        from zerg.tools.tool_search import clear_search_context
        from zerg.tools.tool_search import format_catalog_for_prompt
        from zerg.tools.tool_search import set_search_context

        registry = get_registry()
        allowed_names = [t.name for t in tools]
        lazy_binder = LazyToolBinder(registry, allowed_tools=allowed_names)
        set_search_context(allowed_tools=allowed_names, max_results=8)

        bound_tools = lazy_binder.get_bound_tools()
        tools_by_name = {t.name: t for t in tools}

        # Inject tool catalog into system prompt
        loaded_core_names = sorted(lazy_binder.loaded_tool_names)
        catalog = build_catalog()
        catalog_text = format_catalog_for_prompt(catalog, exclude_core=True)
        instructions = "You have access to the following tools. Core tools are always available."
        if "search_tools" in loaded_core_names:
            instructions += " For other tools, first call `search_tools` with a query describing what you need."
        catalog_header = (
            f"\n\n## Available Tools\n{instructions}\n" f"\n### Core Tools (always loaded): {', '.join(loaded_core_names)}\n{catalog_text}"
        )
        if messages and hasattr(messages[0], "type") and messages[0].type == "system":
            messages = [SystemMessage(content=messages[0].content + catalog_header)] + list(messages[1:])

        logger.info(f"[LazyLoading] {len(bound_tools)} core tools, {len(tools)} total, catalog injected")
    else:
        lazy_binder = None
        bound_tools = tools
        tools_by_name = {t.name: t for t in tools}

    model = fiche_row.model
    cfg = getattr(fiche_row, "config", {}) or {}
    reasoning_effort = (cfg.get("reasoning_effort") or "none").lower()
    reset_llm_usage()

    try:
        llm_with_tools = _make_llm(model=model, tools=bound_tools, reasoning_effort=reasoning_effort)
        current_messages = list(messages)

        # Lazy tool loading: get tool and rebind LLM if needed
        def get_tool_for_execution(tool_name: str) -> BaseTool | None:
            nonlocal llm_with_tools, bound_tools
            if not lazy_binder:
                return tools_by_name.get(tool_name)
            tool = lazy_binder.get_tool(tool_name)
            if lazy_binder.needs_rebind():
                bound_tools = lazy_binder.get_bound_tools()
                llm_with_tools = _make_llm(model=model, tools=bound_tools, reasoning_effort=reasoning_effort)
                lazy_binder.clear_rebind_flag()
                logger.info(f"[LazyLoading] Rebound LLM with {len(bound_tools)} tools after loading '{tool_name}'")
            return tool

        MAX_TOOLS_FROM_SEARCH = 8

        def _maybe_rebind_after_tool_search(tool_results: list[ToolMessage]) -> None:
            """Rebind LLM with tools discovered via search_tools (Claude Code pattern)."""
            nonlocal llm_with_tools, bound_tools
            if not lazy_binder:
                return
            names: list[str] = []
            for msg in tool_results:
                if msg.name != "search_tools":
                    continue
                try:
                    payload = json.loads(msg.content)
                except Exception:
                    continue
                for entry in payload.get("tools") or []:
                    name = entry.get("name")
                    if isinstance(name, str) and name:
                        names.append(name)
            if not names:
                return
            seen: set[str] = set()
            deduped = [n for n in names if not (n in seen or seen.add(n))][:MAX_TOOLS_FROM_SEARCH]
            loaded = lazy_binder.load_tools(deduped)
            if lazy_binder.needs_rebind():
                bound_tools = lazy_binder.get_bound_tools()
                llm_with_tools = _make_llm(model=model, tools=bound_tools, reasoning_effort=reasoning_effort)
                lazy_binder.clear_rebind_flag()
                logger.info(f"[LazyLoading] Rebound after search_tools; loaded={loaded}, total={len(bound_tools)}")

        # Shared LLM call kwargs
        llm_kwargs = dict(run_id=run_id, owner_id=owner_id, model=model, trace_id=trace_id, enable_token_stream=enable_token_stream)

        # Check for pending tool calls (resume case)
        phase = "initial"
        if current_messages:
            last_msg = current_messages[-1]
            if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
                pending_ids = {tc["id"] for tc in last_msg.tool_calls}
                responded_ids = {m.tool_call_id for m in current_messages if isinstance(m, ToolMessage)}
                unresponded = pending_ids - responded_ids

                if unresponded:
                    logger.info(f"Resuming with {len(unresponded)} pending tool call(s)")
                    pending_calls = [tc for tc in last_msg.tool_calls if tc["id"] in unresponded]
                    tool_results, interrupt_value = await _execute_tools_parallel(
                        pending_calls,
                        tools_by_name,
                        run_id=run_id,
                        owner_id=owner_id,
                        tool_getter=get_tool_for_execution if lazy_binder else None,
                    )
                    if interrupt_value:
                        current_messages.extend(tool_results)
                        return OikosResult(
                            messages=current_messages,
                            usage=get_llm_usage(),
                            interrupted=True,
                            interrupt_value=interrupt_value,
                        )
                    current_messages.extend(tool_results)
                    _maybe_rebind_after_tool_search(tool_results)
                    phase = "resume_synthesis"

        llm_response = await _call_llm(current_messages, llm_with_tools, phase=phase, **llm_kwargs)

        # Empty response recovery: retry once with tool_choice=required
        if isinstance(llm_response, AIMessage) and not llm_response.tool_calls and not _extract_text_content(llm_response).strip():
            logger.warning("Fiche produced empty response; retrying once")
            current_messages.append(
                SystemMessage(
                    content=(
                        "Your previous response was empty. You MUST either:\n"
                        "1) Call the appropriate tool(s), OR\n"
                        "2) Provide a final answer.\n\nDo not return an empty message."
                    )
                )
            )
            llm_response = await _call_llm(
                current_messages,
                _make_llm(
                    model=model,
                    tools=bound_tools,
                    reasoning_effort=reasoning_effort,
                    tool_choice="required" if bound_tools else None,
                ),
                phase="empty_retry",
                **llm_kwargs,
            )
            if isinstance(llm_response, AIMessage) and not llm_response.tool_calls and not _extract_text_content(llm_response).strip():
                logger.error("Fiche produced empty response after retry")
                llm_response = AIMessage(content="Error: LLM returned an empty response twice. This is a provider/model issue.")

        # Main ReAct loop with iteration guard
        iteration = 0
        while isinstance(llm_response, AIMessage) and llm_response.tool_calls:
            iteration += 1
            if iteration > MAX_REACT_ITERATIONS:
                logger.error(f"ReAct loop exceeded {MAX_REACT_ITERATIONS} iterations")
                current_messages.append(AIMessage(content=f"Error: Oikos exceeded maximum of {MAX_REACT_ITERATIONS} tool iterations."))
                return OikosResult(messages=current_messages, usage=get_llm_usage())

            current_messages.append(llm_response)

            tool_results, interrupt_value = await _execute_tools_parallel(
                llm_response.tool_calls,
                tools_by_name,
                run_id=run_id,
                owner_id=owner_id,
                tool_getter=get_tool_for_execution if lazy_binder else None,
            )

            if interrupt_value:
                current_messages.extend(tool_results)
                return OikosResult(messages=current_messages, usage=get_llm_usage(), interrupted=True, interrupt_value=interrupt_value)

            current_messages.extend(tool_results)
            _maybe_rebind_after_tool_search(tool_results)

            llm_response = await _call_llm(current_messages, llm_with_tools, phase="tool_iteration", **llm_kwargs)

        current_messages.append(llm_response)
        return OikosResult(messages=current_messages, usage=get_llm_usage())

    finally:
        if lazy_loading:
            clear_search_context()
