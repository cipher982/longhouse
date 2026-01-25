"""LangGraph-free ReAct engine for supervisor agents.

This module provides a pure async ReAct loop for supervisor execution without
LangGraph checkpointing or interrupt() semantics. It replaces the @entrypoint-
decorated graph with explicit control flow.

Key differences from LangGraph-based implementation:
- No checkpointer - state is managed via DB thread messages
- No interrupt() - spawn_commis raises AgentInterrupted directly
- No add_messages() - plain list operations
- Returns (messages, usage) tuple for explicit persistence

Lazy Loading (optional):
- When lazy_loading=True, only core tools are bound initially
- Tool catalog is injected into system prompt for awareness
- Non-core tools are loaded on-demand via LazyToolBinder
- LLM is rebound when new tools are loaded

Usage:
    result = await run_supervisor_loop(
        messages=db_messages,
        agent_row=agent,
        tools=tool_list,
        lazy_loading=True,  # Enable lazy loading
    )
    new_messages = result.messages
    usage = result.usage
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage
from langchain_core.messages import BaseMessage
from langchain_core.messages import SystemMessage
from langchain_core.messages import ToolMessage
from langchain_openai import ChatOpenAI

from zerg.managers.agent_runner import AgentInterrupted

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Usage tracking (thread-local accumulator)
# ---------------------------------------------------------------------------

# Use None as default to avoid mutable default footgun
_llm_usage_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar("llm_usage", default=None)

# Maximum iterations in the ReAct loop to prevent infinite loops
MAX_REACT_ITERATIONS = 50


def _empty_usage() -> dict:
    """Return a fresh empty usage dict."""
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
    }


def reset_llm_usage() -> None:
    """Reset accumulated LLM usage. Call before starting a new run."""
    # Keep as None until we observe real usage metadata from the provider.
    # This preserves legacy semantics where missing usage stays NULL in DB.
    _llm_usage_var.set(None)


def get_llm_usage() -> dict:
    """Get accumulated LLM usage from current run."""
    usage = _llm_usage_var.get()
    if usage is None:
        return {}
    return usage


def _accumulate_llm_usage(usage: dict) -> None:
    """Add usage from an LLM call to the accumulated total."""
    current = _llm_usage_var.get()
    if current is None:
        current = _empty_usage()

    current["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
    current["completion_tokens"] += usage.get("completion_tokens", 0) or 0
    current["total_tokens"] += usage.get("total_tokens", 0) or 0

    # Extract reasoning_tokens from completion_tokens_details
    details = usage.get("completion_tokens_details") or {}
    current["reasoning_tokens"] += details.get("reasoning_tokens", 0) or 0

    _llm_usage_var.set(current)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class SupervisorResult:
    """Result from supervisor ReAct loop execution."""

    messages: list[BaseMessage]
    """Full message history including new messages from this run."""

    usage: dict = field(default_factory=dict)
    """Accumulated token usage for this run."""

    interrupted: bool = False
    """True if execution was interrupted (spawn_commis called)."""

    interrupt_value: dict | None = None
    """Interrupt payload if interrupted=True."""


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
    """Create a tool-bound ChatOpenAI instance."""
    from zerg.config import get_settings
    from zerg.testing.test_models import is_test_model
    from zerg.testing.test_models import warn_if_test_model

    # Handle mock/scripted models for testing
    if is_test_model(model):
        warn_if_test_model(model)

        if model == "gpt-mock":
            from zerg.testing.mock_llm import MockChatLLM

            llm = MockChatLLM()
            try:
                return llm.bind_tools(tools, tool_choice=tool_choice)
            except TypeError:
                return llm.bind_tools(tools)

        if model == "gpt-scripted":
            # ScriptedChatLLM not typically used for supervisor
            from zerg.testing.scripted_llm import ScriptedChatLLM

            llm = ScriptedChatLLM(sequences=[])
            try:
                return llm.bind_tools(tools, tool_choice=tool_choice)
            except TypeError:
                return llm.bind_tools(tools)

    # Look up model config for provider routing
    from zerg.models_config import ModelProvider
    from zerg.models_config import get_all_models
    from zerg.models_config import get_model_by_id

    model_config = get_model_by_id(model)
    settings = get_settings()

    # Validate model exists
    if not model_config:
        available = [m.id for m in get_all_models()]
        raise ValueError(f"Unknown model: {model}. Available: {available}")

    # Select API key and base_url based on provider
    provider = model_config.provider

    if provider == ModelProvider.GROQ:
        api_key = settings.groq_api_key
        base_url = model_config.base_url
        # Validate Groq API key exists
        if not api_key:
            raise ValueError(f"GROQ_API_KEY not configured but Groq model '{model}' selected")
    else:
        api_key = settings.openai_api_key
        base_url = None

    kwargs: dict = {
        "model": model,
        "streaming": settings.llm_token_stream,
        "api_key": api_key,
    }

    # Check if model supports reasoning
    capabilities = model_config.capabilities or {}
    supports_reasoning = capabilities.get("reasoning", False)
    supports_reasoning_none = capabilities.get("reasoningNone", False)

    # Add base_url and provider-specific config
    if provider == ModelProvider.GROQ:
        kwargs["base_url"] = base_url

    # Only pass reasoning_effort if model supports it
    if supports_reasoning:
        # If model doesn't support 'none', use 'low' as fallback
        effort = reasoning_effort
        if reasoning_effort == "none" and not supports_reasoning_none:
            effort = "low"
        kwargs["reasoning_effort"] = effort

    llm = ChatOpenAI(**kwargs)

    if tool_choice is None:
        return llm.bind_tools(tools)

    try:
        return llm.bind_tools(tools, tool_choice=tool_choice)
    except TypeError:
        return llm.bind_tools(tools)


# ---------------------------------------------------------------------------
# Heartbeat emission during LLM calls
# ---------------------------------------------------------------------------


async def _emit_heartbeats(
    heartbeat_cancelled: asyncio.Event,
    run_id: int | None,
    owner_id: int | None,
    phase: str,
) -> None:
    """Emit heartbeats every 10 seconds during LLM call."""
    from zerg.events import EventType
    from zerg.events import event_bus

    try:
        while not heartbeat_cancelled.is_set():
            await asyncio.sleep(10)
            if heartbeat_cancelled.is_set():
                break

            if run_id is not None:
                await event_bus.publish(
                    EventType.SUPERVISOR_HEARTBEAT,
                    {
                        "event_type": EventType.SUPERVISOR_HEARTBEAT,
                        "run_id": run_id,
                        "owner_id": owner_id,
                        "activity": "llm_reasoning",
                        "phase": phase,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )
                logger.debug(f"Emitted heartbeat for supervisor run {run_id} during {phase}")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning(f"Error in heartbeat task: {e}")


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

    start_time = datetime.now(timezone.utc)

    # Start heartbeat task - must be inside try to ensure cleanup
    heartbeat_cancelled = asyncio.Event()
    heartbeat_task = asyncio.create_task(_emit_heartbeats(heartbeat_cancelled, run_id, owner_id, phase))

    # Initialize to avoid UnboundLocalError if log_request fails
    audit_correlation_id = None

    try:
        # Audit log request (inside try to ensure heartbeat cleanup on failure)
        audit_correlation_id = await audit_logger.log_request(
            run_id=run_id,
            worker_id=None,
            owner_id=owner_id,
            trace_id=trace_id,
            phase=phase,
            model=model,
            messages=messages,
        )
        if enable_token_stream:
            from zerg.callbacks.token_stream import WsTokenCallback

            callback = WsTokenCallback()
            result = await llm_with_tools.ainvoke(messages, config={"callbacks": [callback]})
        else:
            result = await llm_with_tools.ainvoke(messages)

    except Exception as e:
        # Audit log error (only if we got a correlation_id)
        error_duration = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)
        if audit_correlation_id is not None:
            try:
                await audit_logger.log_response(
                    correlation_id=audit_correlation_id,
                    content=None,
                    tool_calls=None,
                    input_tokens=None,
                    output_tokens=None,
                    reasoning_tokens=None,
                    duration_ms=error_duration,
                    error=str(e),
                )
            except Exception as log_err:
                logger.warning(f"Failed to log audit error: {log_err}")
        raise

    finally:
        # Stop heartbeat
        heartbeat_cancelled.set()
        try:
            await asyncio.wait_for(heartbeat_task, timeout=1.0)
        except asyncio.TimeoutError:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    end_time = datetime.now(timezone.utc)
    duration_ms = int((end_time - start_time).total_seconds() * 1000)

    # Audit log response
    try:
        audit_content = None
        audit_tool_calls = None
        audit_usage = None

        if isinstance(result, AIMessage):
            audit_content = result.content
            audit_tool_calls = result.tool_calls
            audit_usage = getattr(result, "usage_metadata", {}) or {}

        await audit_logger.log_response(
            correlation_id=audit_correlation_id,
            content=audit_content,
            tool_calls=audit_tool_calls,
            input_tokens=audit_usage.get("input_tokens") if audit_usage else None,
            output_tokens=audit_usage.get("output_tokens") if audit_usage else None,
            reasoning_tokens=(audit_usage.get("output_token_details", {}).get("reasoning") if audit_usage else None),
            duration_ms=duration_ms,
        )
    except Exception as e:
        logger.warning(f"Failed to log audit response: {e}")

    # Accumulate usage
    if isinstance(result, AIMessage):
        usage_meta = getattr(result, "usage_metadata", None)
        if usage_meta:
            usage_dict = {
                "prompt_tokens": usage_meta.get("input_tokens", 0),
                "completion_tokens": usage_meta.get("output_tokens", 0),
                "total_tokens": usage_meta.get("total_tokens", 0),
                "completion_tokens_details": {"reasoning_tokens": usage_meta.get("output_token_details", {}).get("reasoning", 0)},
            }
            _accumulate_llm_usage(usage_dict)

    return result


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

    For spawn_commis, raises AgentInterrupted instead of returning ToolMessage.
    For other tools, returns ToolMessage with result.

    Args:
        tool_call: Tool call dict with name, args, id.
        tools_by_name: Dict mapping tool names to BaseTool instances.
        run_id: Supervisor run ID for event correlation.
        owner_id: Owner ID for event correlation.
        tool_getter: Optional callable for lazy tool loading. If provided,
            called with tool_name to get/load the tool. Used for lazy loading.

    Raises:
        AgentInterrupted: If spawn_commis is called and job is queued (not already complete).
    """
    import json

    from zerg.events import get_emitter
    from zerg.tools.result_utils import check_tool_error
    from zerg.tools.result_utils import redact_sensitive_args
    from zerg.tools.result_utils import safe_preview

    tool_name = tool_call.get("name", "unknown_tool")
    tool_args = tool_call.get("args", {})
    tool_call_id = tool_call.get("id", "")

    # Get emitter for event emission
    emitter = get_emitter()

    # Redact sensitive fields
    safe_args = redact_sensitive_args(tool_args)

    # Emit STARTED event
    if emitter:
        await emitter.emit_tool_started(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_args_preview=safe_preview(str(safe_args)),
            tool_args=safe_args,
        )

    start_time = datetime.now(timezone.utc)
    result_content = None  # May be set by spawn_commis or error handling
    observation = None  # Set by normal tool execution

    # Get tool using tool_getter (lazy loading) or tools_by_name (eager)
    if tool_getter is not None:
        tool_to_call = tool_getter(tool_name)
    else:
        tool_to_call = tools_by_name.get(tool_name)

    if not tool_to_call:
        result_content = f"Error: Tool '{tool_name}' not found."
        logger.error(result_content)
    else:
        try:
            # Special handling for spawn_commis - needs interrupt handling
            if tool_name == "spawn_commis":
                # Import here to avoid circular dependency
                from zerg.tools.builtin.supervisor_tools import spawn_commis_async

                # Call spawn_commis_async directly with tool_call_id for idempotency
                # Pass _skip_interrupt=True because we handle interrupt ourselves
                # Pass _return_structured=True to get job_id directly without regex
                job_result = await spawn_commis_async(
                    task=tool_args.get("task", ""),
                    model=tool_args.get("model"),
                    _tool_call_id=tool_call_id,
                    _skip_interrupt=True,  # LangGraph-free path
                    _return_structured=True,  # Get dict with job_id directly
                )

                # Handle structured response (dict) or string response
                if isinstance(job_result, dict):
                    # Structured response: {"job_id": X, "status": "queued", "task": ...}
                    job_id = job_result.get("job_id")
                    if job_result.get("status") == "queued" and job_id is not None:
                        # Emit tool completion before interrupting
                        end_time = datetime.now(timezone.utc)
                        duration_ms = int((end_time - start_time).total_seconds() * 1000)
                        if emitter:
                            await emitter.emit_tool_completed(
                                tool_name=tool_name,
                                tool_call_id=tool_call_id,
                                duration_ms=duration_ms,
                                result_preview=f"Worker job {job_id} spawned",
                                result=str(job_result),
                            )

                        # Raise interrupt to pause supervisor
                        raise AgentInterrupted(
                            {
                                "type": "worker_pending",
                                "job_id": job_id,
                                "task": tool_args.get("task", "")[:100],
                                "model": tool_args.get("model"),
                                "tool_call_id": tool_call_id,
                            }
                        )
                    else:
                        # Unexpected dict response (shouldn't happen with _return_structured=True)
                        result_content = json.dumps(job_result)
                else:
                    # String response - typically an error or completed result
                    result_content = str(job_result)

            # Special handling for spawn_workspace_commis - needs interrupt handling
            elif tool_name == "spawn_workspace_commis":
                from zerg.tools.builtin.supervisor_tools import spawn_workspace_commis_async

                job_result = await spawn_workspace_commis_async(
                    task=tool_args.get("task", ""),
                    git_repo=tool_args.get("git_repo", ""),
                    model=tool_args.get("model"),
                    resume_session_id=tool_args.get("resume_session_id"),
                    _tool_call_id=tool_call_id,
                    _return_structured=True,
                )

                # Handle structured response (dict) or string response
                if isinstance(job_result, dict):
                    job_id = job_result.get("job_id")
                    if job_result.get("status") == "queued" and job_id is not None:
                        end_time = datetime.now(timezone.utc)
                        duration_ms = int((end_time - start_time).total_seconds() * 1000)
                        if emitter:
                            await emitter.emit_tool_completed(
                                tool_name=tool_name,
                                tool_call_id=tool_call_id,
                                duration_ms=duration_ms,
                                result_preview=f"Worker job {job_id} spawned",
                                result=str(job_result),
                            )
                        raise AgentInterrupted(
                            {
                                "type": "worker_pending",
                                "job_id": job_id,
                                "task": tool_args.get("task", "")[:100],
                                "model": tool_args.get("model"),
                                "tool_call_id": tool_call_id,
                            }
                        )
                    else:
                        result_content = json.dumps(job_result)
                else:
                    result_content = str(job_result)

            # Special handling for spawn_standard_worker - needs interrupt handling
            elif tool_name == "spawn_standard_worker":
                from zerg.tools.builtin.supervisor_tools import spawn_standard_worker_async

                job_result = await spawn_standard_worker_async(
                    task=tool_args.get("task", ""),
                    model=tool_args.get("model"),
                    _tool_call_id=tool_call_id,
                    _return_structured=True,
                )

                # Handle structured response (dict) or string response
                if isinstance(job_result, dict):
                    job_id = job_result.get("job_id")
                    if job_result.get("status") == "queued" and job_id is not None:
                        end_time = datetime.now(timezone.utc)
                        duration_ms = int((end_time - start_time).total_seconds() * 1000)
                        if emitter:
                            await emitter.emit_tool_completed(
                                tool_name=tool_name,
                                tool_call_id=tool_call_id,
                                duration_ms=duration_ms,
                                result_preview=f"Worker job {job_id} spawned",
                                result=str(job_result),
                            )
                        raise AgentInterrupted(
                            {
                                "type": "worker_pending",
                                "job_id": job_id,
                                "task": tool_args.get("task", "")[:100],
                                "model": tool_args.get("model"),
                                "tool_call_id": tool_call_id,
                            }
                        )
                    else:
                        result_content = json.dumps(job_result)
                else:
                    result_content = str(job_result)

            # Special handling for wait_for_commis (needs tool_call_id for resume)
            elif tool_name == "wait_for_commis":
                from zerg.tools.builtin.supervisor_tools import wait_for_commis_async

                # Pass tool_call_id for proper resume handling
                observation = await wait_for_commis_async(
                    job_id=tool_args.get("job_id", ""),
                    _tool_call_id=tool_call_id,
                )

            # Check if tool has async implementation
            elif getattr(tool_to_call, "coroutine", None):
                observation = await tool_to_call.ainvoke(tool_args)
            else:
                # Run sync tool in thread
                observation = await asyncio.to_thread(tool_to_call.invoke, tool_args)

            # Serialize observation (only if not already set by spawn_commis)
            if observation is not None and result_content is None:
                if isinstance(observation, dict):
                    from datetime import date as date_type

                    def datetime_handler(obj):
                        if isinstance(obj, (datetime, date_type)):
                            return obj.isoformat()
                        raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")

                    result_content = json.dumps(observation, default=datetime_handler)
                else:
                    result_content = str(observation)

        except AgentInterrupted:
            # Re-raise interrupt
            raise
        except Exception as exc:
            result_content = f"<tool-error> {exc}"
            logger.exception("Error executing tool %s", tool_name)

    end_time = datetime.now(timezone.utc)
    duration_ms = int((end_time - start_time).total_seconds() * 1000)

    # Defensive: ensure result_content is not None
    if result_content is None:
        result_content = "(No result)"

    raw_result_content = str(result_content)

    # Check for errors on the raw content (before any truncation)
    is_error, error_msg = check_tool_error(raw_result_content)

    # Optionally store large tool outputs out-of-band
    from zerg.config import get_settings
    from zerg.services.tool_output_store import ToolOutputStore

    settings = get_settings()
    max_chars = max(0, int(settings.supervisor_tool_output_max_chars or 0))
    preview_chars = max(0, int(settings.supervisor_tool_output_preview_chars or 0))

    result_content = raw_result_content

    should_store = max_chars > 0 and len(raw_result_content) > max_chars and tool_name != "get_tool_output"

    if should_store:
        if preview_chars <= 0:
            preview_chars = min(200, max_chars)
        else:
            preview_chars = min(preview_chars, max_chars)

        stored = False
        artifact_id = None
        store_reason = None

        if owner_id is None:
            store_reason = "no owner_id available"
        else:
            try:
                store = ToolOutputStore()
                artifact_id = store.save_output(
                    owner_id=owner_id,
                    tool_name=tool_name,
                    content=raw_result_content,
                    run_id=run_id,
                    tool_call_id=tool_call_id,
                )
                stored = True
            except Exception:
                store_reason = "storage failed"
                logger.exception("Failed to store tool output for %s", tool_name)

        preview = raw_result_content[:preview_chars]

        if stored and artifact_id:
            size_bytes = len(raw_result_content.encode("utf-8"))
            marker = f"[TOOL_OUTPUT:artifact_id={artifact_id},tool={tool_name},bytes={size_bytes}]"
            error_line = ""
            if is_error:
                error_line = f"\nTool error detected: {safe_preview(error_msg or raw_result_content, 500)}"

            result_content = (
                f"{marker}\n"
                f"Tool output exceeded {max_chars} characters and was stored out of band."
                f"{error_line}\n"
                f"Preview (first {preview_chars} chars):\n"
                f"{preview}\n\n"
                "Use get_tool_output(artifact_id) to fetch the full output."
            )
        else:
            reason_line = "Full output was not stored."
            if store_reason:
                reason_line = f"Full output was not stored ({store_reason})."
            result_content = (
                f"(Tool output truncated; exceeded {max_chars} characters.)\n"
                f"{reason_line}\n"
                f"Preview (first {preview_chars} chars):\n"
                f"{preview}"
            )

    # Emit COMPLETED/FAILED event
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
    """Execute tools in parallel, handling spawn_commiss specially.

    Implements the parallel-first pattern:
    1. Non-spawn tools execute concurrently via asyncio.gather()
    2. Spawn_worker calls are collected (not executed immediately)
    3. Returns interrupt info with ALL spawn_commis job_ids for barrier creation

    Two-Phase Commit for spawn_commis:
    - Jobs are created with status='created' (not 'queued')
    - Caller (supervisor_service) creates WorkerBarrier + flips to 'queued'
    - This prevents the "fast worker" race condition

    Args:
        tool_calls: List of tool call dicts from LLM response.
        tools_by_name: Dict mapping tool names to BaseTool instances.
        run_id: Supervisor run ID for event correlation.
        owner_id: Owner ID for event correlation.
        tool_getter: Optional callable for lazy tool loading.

    Returns:
        Tuple of (tool_results, interrupt_value):
        - tool_results: List of ToolMessages from non-spawn tools
        - interrupt_value: Dict with spawn_commis info if any, None otherwise

    Note:
        Does NOT raise AgentInterrupted - caller handles interruption.
    """

    # Separate spawn_commiss from other tools
    spawn_calls = [tc for tc in tool_calls if tc.get("name") == "spawn_commis"]
    other_calls = [tc for tc in tool_calls if tc.get("name") != "spawn_commis"]

    tool_results: list[ToolMessage] = []

    # Phase 1: Execute non-spawn tools in parallel
    if other_calls:

        async def execute_single_tool(tc: dict) -> ToolMessage:
            """Execute a single tool, catching exceptions."""
            try:
                return await _execute_tool(
                    tc,
                    tools_by_name,
                    run_id=run_id,
                    owner_id=owner_id,
                    tool_getter=tool_getter,
                )
            except AgentInterrupted:
                # Re-raise - shouldn't happen for non-spawn tools
                raise
            except Exception as exc:
                logger.exception(f"Error in parallel tool execution: {tc.get('name')}")
                return ToolMessage(
                    content=f"<tool-error>{exc}</tool-error>",
                    tool_call_id=tc.get("id", ""),
                    name=tc.get("name", "unknown"),
                )

        # Execute all non-spawn tools concurrently
        results = await asyncio.gather(
            *[execute_single_tool(tc) for tc in other_calls],
            return_exceptions=True,
        )

        # Process results, preserving order
        # CRITICAL: Check for AgentInterrupted first - it must propagate, not become a ToolMessage
        from zerg.managers.agent_runner import AgentInterrupted

        for tc, result in zip(other_calls, results):
            if isinstance(result, AgentInterrupted):
                # Re-raise interrupt (e.g., from wait_for_worker)
                # This ensures the supervisor enters WAITING state
                logger.info(f"[INTERRUPT] Re-raising AgentInterrupted from {tc.get('name')}")
                raise result
            elif isinstance(result, Exception):
                # Shouldn't happen often since execute_single_tool catches exceptions
                tool_results.append(
                    ToolMessage(
                        content=f"<tool-error>{result}</tool-error>",
                        tool_call_id=tc.get("id", ""),
                        name=tc.get("name", "unknown"),
                    )
                )
            else:
                tool_results.append(result)

    # Phase 2: Process spawn_commiss (two-phase commit pattern)
    if spawn_calls:
        import time

        from zerg.connectors.context import get_credential_resolver
        from zerg.events.supervisor_emitter import SupervisorEmitter
        from zerg.models.models import WorkerJob
        from zerg.services.supervisor_context import get_supervisor_context

        # Get context for job creation
        resolver = get_credential_resolver()
        ctx = get_supervisor_context()

        # Create emitter for tool lifecycle events
        emitter = None
        if ctx:
            emitter = SupervisorEmitter(
                run_id=ctx.run_id,
                owner_id=ctx.owner_id,
                message_id=ctx.message_id,
                trace_id=ctx.trace_id,
            )

        if not resolver:
            # No credential context - return error for each spawn_commis
            for tc in spawn_calls:
                tool_results.append(
                    ToolMessage(
                        content="<tool-error>Cannot spawn worker - no credential context</tool-error>",
                        tool_call_id=tc.get("id", ""),
                        name="spawn_commis",
                    )
                )
            return tool_results, None

        db = resolver.db
        supervisor_run_id = ctx.run_id if ctx else None
        trace_id = ctx.trace_id if ctx else None

        # Worker inherits model and reasoning_effort from supervisor context
        worker_model = (ctx.model if ctx else None) or "gpt-5-mini"
        worker_reasoning_effort = (ctx.reasoning_effort if ctx else None) or "none"

        created_jobs: list[dict] = []

        for tc in spawn_calls:
            task = tc.get("args", {}).get("task", "")
            model_override = tc.get("args", {}).get("model")
            tool_call_id = tc.get("id", "")
            start_time = time.time()

            # Emit tool_started event for UI
            if emitter:
                await emitter.emit_tool_started(
                    tool_name="spawn_commis",
                    tool_call_id=tool_call_id,
                    tool_args_preview=task[:100] if task else "",
                    tool_args={"task": task, "model": model_override},
                )

            try:
                # Check for existing job with same tool_call_id (idempotency)
                existing_job = None
                if tool_call_id and supervisor_run_id:
                    existing_job = (
                        db.query(WorkerJob)
                        .filter(
                            WorkerJob.supervisor_run_id == supervisor_run_id,
                            WorkerJob.tool_call_id == tool_call_id,
                        )
                        .first()
                    )

                if existing_job and existing_job.status == "success":
                    # Already completed - return cached result
                    from zerg.services.worker_artifact_store import WorkerArtifactStore

                    artifact_store = WorkerArtifactStore()
                    try:
                        metadata = artifact_store.get_worker_metadata(existing_job.worker_id)
                        summary = metadata.get("summary")
                        result = summary or artifact_store.get_worker_result(existing_job.worker_id)
                        tool_results.append(
                            ToolMessage(
                                content=f"Worker job {existing_job.id} completed:\n\n{result}",
                                tool_call_id=tool_call_id,
                                name="spawn_commis",
                            )
                        )
                        # Emit tool_completed for idempotent cached result
                        if emitter:
                            duration_ms = int((time.time() - start_time) * 1000)
                            await emitter.emit_tool_completed(
                                tool_name="spawn_commis",
                                tool_call_id=tool_call_id,
                                duration_ms=duration_ms,
                                result_preview=f"Cached result for job {existing_job.id}",
                                result={"job_id": existing_job.id, "status": "success", "cached": True},
                            )
                        continue  # Skip to next spawn_commis
                    except FileNotFoundError:
                        pass  # Fall through to create new job

                if existing_job and existing_job.status in ["queued", "running", "created"]:
                    # Reuse existing job
                    created_jobs.append(
                        {
                            "job": existing_job,
                            "tool_call_id": tool_call_id,
                            "task": task[:100],
                        }
                    )
                    # Emit tool_completed for reused job (include job_id for frontend mapping)
                    if emitter:
                        duration_ms = int((time.time() - start_time) * 1000)
                        await emitter.emit_tool_completed(
                            tool_name="spawn_commis",
                            tool_call_id=tool_call_id,
                            duration_ms=duration_ms,
                            result_preview=f"Reusing existing job {existing_job.id}",
                            result={"job_id": existing_job.id, "status": existing_job.status, "task": task[:100]},
                        )
                    continue

                # Create new job with status='created' (TWO-PHASE COMMIT)
                # Workers won't pick up jobs with status='created'
                import uuid as uuid_module

                worker_job = WorkerJob(
                    owner_id=resolver.owner_id,
                    supervisor_run_id=supervisor_run_id,
                    tool_call_id=tool_call_id,
                    trace_id=uuid_module.UUID(trace_id) if trace_id else None,
                    task=task,
                    model=model_override or worker_model,
                    reasoning_effort=worker_reasoning_effort,
                    status="created",  # NOT 'queued' - two-phase commit pattern
                )
                db.add(worker_job)
                db.commit()
                db.refresh(worker_job)

                logger.info(f"[PARALLEL-SPAWN] Created worker job {worker_job.id} with status='created'")

                created_jobs.append(
                    {
                        "job": worker_job,
                        "tool_call_id": tool_call_id,
                        "task": task[:100],
                    }
                )

                # Emit tool_completed for new job (include job_id for frontend mapping)
                if emitter:
                    duration_ms = int((time.time() - start_time) * 1000)
                    await emitter.emit_tool_completed(
                        tool_name="spawn_commis",
                        tool_call_id=tool_call_id,
                        duration_ms=duration_ms,
                        result_preview=f"Created job {worker_job.id}",
                        result={"job_id": worker_job.id, "status": "created", "task": task[:100]},
                    )

            except Exception as exc:
                logger.exception(f"Error creating spawn_commis job: {task[:50]}")
                db.rollback()  # Clear error state so subsequent operations work

                # Emit tool_failed event
                if emitter:
                    duration_ms = int((time.time() - start_time) * 1000)
                    await emitter.emit_tool_failed(
                        tool_name="spawn_commis",
                        tool_call_id=tool_call_id,
                        duration_ms=duration_ms,
                        error=str(exc),
                    )

                tool_results.append(
                    ToolMessage(
                        content=f"<tool-error>Failed to spawn worker: {exc}</tool-error>",
                        tool_call_id=tool_call_id,
                        name="spawn_commis",
                    )
                )

        # If we created/found jobs, return ToolMessages and continue (async model)
        # Workers run in background, supervisor sees results via inbox context on next turn
        if created_jobs:
            from zerg.services.event_store import append_run_event

            for job_info in created_jobs:
                job = job_info["job"]
                tool_call_id = job_info["tool_call_id"]
                task = job_info.get("task", job.task[:100] if job.task else "")
                tool_results.append(
                    ToolMessage(
                        content=f"Worker job {job.id} spawned successfully. Working on: {task}\n\n"
                        f"The worker is running in the background. You can continue the conversation. "
                        f"Check commis status with check_commis_status({job.id}) or wait for results "
                        f"with wait_for_commis({job.id}).",
                        tool_call_id=tool_call_id,
                        name="spawn_commis",
                    )
                )

                # Flip job status from 'created' to 'queued' immediately
                db.query(WorkerJob).filter(
                    WorkerJob.id == job.id,
                    WorkerJob.status == "created",
                ).update({"status": "queued"})

            db.commit()

            # Emit worker_spawned events for UI
            if supervisor_run_id is not None:
                for job_info in created_jobs:
                    job = job_info["job"]
                    tool_call_id = job_info["tool_call_id"]
                    task = job_info.get("task", job.task[:100] if job.task else "")
                    await append_run_event(
                        run_id=supervisor_run_id,
                        event_type="worker_spawned",
                        payload={
                            "job_id": job.id,
                            "tool_call_id": tool_call_id,
                            "task": task,
                            "model": job.model,
                            "owner_id": resolver.owner_id,
                            "trace_id": trace_id,
                        },
                    )

            logger.info(f"Spawned {len(created_jobs)} workers (async)")
            return tool_results, None

    return tool_results, None


# ---------------------------------------------------------------------------
# Main ReAct Loop
# ---------------------------------------------------------------------------


async def run_supervisor_loop(
    messages: list[BaseMessage],
    agent_row,
    tools: list[BaseTool],
    *,
    run_id: int | None = None,
    owner_id: int | None = None,
    trace_id: str | None = None,
    enable_token_stream: bool = False,
    lazy_loading: bool = False,
) -> SupervisorResult:
    """Run the supervisor ReAct loop until completion or interrupt.

    This is the main entry point for LangGraph-free supervisor execution.

    Args:
        messages: Initial message history (from DB).
        agent_row: Agent ORM row or AgentRuntimeView with model config.
        tools: List of available tools.
        run_id: Supervisor run ID for event correlation.
        owner_id: Owner ID for event correlation.
        trace_id: End-to-end trace ID for debugging.
        enable_token_stream: Whether to stream tokens.
        lazy_loading: If True, use lazy tool loading with catalog injection.
            Core tools are always bound; other tools load on-demand.

    Returns:
        SupervisorResult with messages, usage, and interrupt status.
        If interrupted=True, caller should persist messages and set run to WAITING.
    """
    # Set up tool binder (lazy or eager)
    if lazy_loading:
        from zerg.tools.catalog import build_catalog
        from zerg.tools.catalog import format_catalog_for_prompt
        from zerg.tools.lazy_binder import LazyToolBinder
        from zerg.tools.tool_search import clear_search_context
        from zerg.tools.tool_search import set_search_context
        from zerg.tools.unified_access import get_tool_resolver

        # Build lazy binder from resolver (not pre-filtered tools)
        resolver = get_tool_resolver()
        # Extract allowlist from tools if filtering was applied
        allowed_names = [t.name for t in tools]
        lazy_binder = LazyToolBinder(resolver, allowed_tools=allowed_names)

        # Set search context so search_tools respects allowlist and rebind cap
        # MAX_TOOLS_FROM_SEARCH is defined below in _maybe_rebind_after_tool_search
        set_search_context(allowed_tools=allowed_names, max_results=8)

        # Use only loaded tools for binding
        bound_tools = lazy_binder.get_bound_tools()
        tools_by_name = {t.name: t for t in tools}  # Full set for execution

        # Inject catalog into first system message
        # Use actually loaded core tools (respects allowlist) not the full CORE_TOOLS set
        loaded_core_names = sorted(lazy_binder.loaded_tool_names)
        catalog = build_catalog()
        catalog_text = format_catalog_for_prompt(catalog, exclude_core=True)
        catalog_instructions = "You have access to the following tools. Core tools are always available."
        if "search_tools" in loaded_core_names:
            catalog_instructions += (
                " For other tools, first call `search_tools` with a query describing what you need. "
                "The matching tools will be available on your next turn."
            )

        catalog_header = (
            "\n\n## Available Tools\n"
            f"{catalog_instructions}\n"
            f"\n### Core Tools (always loaded): {', '.join(loaded_core_names)}\n"
            f"{catalog_text}"
        )

        # Inject catalog after first system message
        if messages and hasattr(messages[0], "type") and messages[0].type == "system":
            original_content = messages[0].content
            messages = [SystemMessage(content=original_content + catalog_header)] + list(messages[1:])

        logger.info(
            f"[LazyLoading] Initialized with {len(bound_tools)} core tools, " f"{len(tools)} total tools available, catalog injected"
        )
    else:
        # Eager loading - all tools bound upfront (original behavior)
        lazy_binder = None
        bound_tools = tools
        tools_by_name = {tool.name: tool for tool in tools}

    # Get model and reasoning effort from agent config
    model = agent_row.model
    cfg = getattr(agent_row, "config", {}) or {}
    reasoning_effort = (cfg.get("reasoning_effort") or "none").lower()

    # Reset usage tracking
    reset_llm_usage()

    try:
        # Create LLM with bound tools (core-only for lazy, all for eager)
        llm_with_tools = _make_llm(
            model=model,
            tools=bound_tools,
            reasoning_effort=reasoning_effort,
        )

        current_messages = list(messages)  # Copy to avoid mutation

        # Helper to get tool and handle lazy loading
        def get_tool_for_execution(tool_name: str) -> BaseTool | None:
            """Get a tool for execution, handling lazy loading if enabled."""
            nonlocal llm_with_tools, bound_tools

            if lazy_binder:
                tool = lazy_binder.get_tool(tool_name)
                # Check if we need to rebind (new tools were loaded)
                if lazy_binder.needs_rebind():
                    bound_tools = lazy_binder.get_bound_tools()
                    llm_with_tools = _make_llm(
                        model=model,
                        tools=bound_tools,
                        reasoning_effort=reasoning_effort,
                    )
                    lazy_binder.clear_rebind_flag()
                    logger.info(f"[LazyLoading] Rebound LLM with {len(bound_tools)} tools after loading '{tool_name}'")
                return tool
            else:
                return tools_by_name.get(tool_name)

        # Maximum tools to load from a single search_tools call
        MAX_TOOLS_FROM_SEARCH = 8

        def _maybe_rebind_after_tool_search(tool_results: list[ToolMessage]) -> None:
            """Rebind LLM with tools discovered via search_tools.

            This implements the Claude Code pattern: after search_tools returns,
            we parse the tool names and bind them BEFORE the next LLM call.
            This allows the LLM to actually call the discovered tools.
            """
            nonlocal llm_with_tools, bound_tools

            if not lazy_binder:
                return

            # Collect tool names returned by search_tools
            names: list[str] = []
            for msg in tool_results:
                if msg.name != "search_tools":
                    continue
                try:
                    payload = json.loads(msg.content)
                except Exception:
                    logger.debug("[LazyLoading] search_tools result not JSON; skipping")
                    continue

                for entry in payload.get("tools") or []:
                    name = entry.get("name")
                    if isinstance(name, str) and name:
                        names.append(name)

            if not names:
                return

            # De-dupe and cap to prevent context explosion
            seen: set[str] = set()
            deduped: list[str] = []
            for name in names:
                if name not in seen:
                    seen.add(name)
                    deduped.append(name)
            deduped = deduped[:MAX_TOOLS_FROM_SEARCH]

            loaded = lazy_binder.load_tools(deduped)
            if lazy_binder.needs_rebind():
                bound_tools = lazy_binder.get_bound_tools()
                llm_with_tools = _make_llm(
                    model=model,
                    tools=bound_tools,
                    reasoning_effort=reasoning_effort,
                )
                lazy_binder.clear_rebind_flag()
                logger.info(f"[LazyLoading] Rebound after search_tools; loaded={loaded}, total bound={len(bound_tools)}")

        # Check for pending tool calls (resume case)
        if current_messages:
            last_msg = current_messages[-1]
            if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
                pending_tool_ids = {tc["id"] for tc in last_msg.tool_calls}
                responded_tool_ids = {m.tool_call_id for m in current_messages if isinstance(m, ToolMessage)}
                unresponded = pending_tool_ids - responded_tool_ids

                if unresponded:
                    # Resume: execute pending tools in PARALLEL
                    logger.info(f"Resuming with {len(unresponded)} pending tool call(s)")
                    pending_calls = [tc for tc in last_msg.tool_calls if tc["id"] in unresponded]

                    tool_results, interrupt_value = await _execute_tools_parallel(
                        pending_calls,
                        tools_by_name,
                        run_id=run_id,
                        owner_id=owner_id,
                        tool_getter=get_tool_for_execution if lazy_binder else None,
                    )

                    # Handle interruption from spawn_commis (barrier pattern)
                    if interrupt_value:
                        current_messages.extend(tool_results)
                        return SupervisorResult(
                            messages=current_messages,
                            usage=get_llm_usage(),
                            interrupted=True,
                            interrupt_value=interrupt_value,
                        )

                    current_messages.extend(tool_results)

                    # Rebind tools if search_tools was called (Claude Code pattern)
                    _maybe_rebind_after_tool_search(tool_results)

                    # Call LLM with tool results
                    llm_response = await _call_llm(
                        current_messages,
                        llm_with_tools,
                        phase="resume_synthesis",
                        run_id=run_id,
                        owner_id=owner_id,
                        model=model,
                        trace_id=trace_id,
                        enable_token_stream=enable_token_stream,
                    )
                else:
                    # All tool calls responded, proceed normally
                    llm_response = await _call_llm(
                        current_messages,
                        llm_with_tools,
                        phase="initial",
                        run_id=run_id,
                        owner_id=owner_id,
                        model=model,
                        trace_id=trace_id,
                        enable_token_stream=enable_token_stream,
                    )
            else:
                # No pending tool calls
                llm_response = await _call_llm(
                    current_messages,
                    llm_with_tools,
                    phase="initial",
                    run_id=run_id,
                    owner_id=owner_id,
                    model=model,
                    trace_id=trace_id,
                    enable_token_stream=enable_token_stream,
                )
        else:
            # Empty messages (shouldn't happen in production)
            llm_response = await _call_llm(
                current_messages,
                llm_with_tools,
                phase="initial",
                run_id=run_id,
                owner_id=owner_id,
                model=model,
                trace_id=trace_id,
                enable_token_stream=enable_token_stream,
            )

        # Handle empty response retry
        if isinstance(llm_response, AIMessage) and not llm_response.tool_calls:
            content = llm_response.content
            content_text = ""
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        content_text += str(part.get("text") or "")
                    elif isinstance(part, str):
                        content_text += part
            else:
                content_text = str(content or "")

            if not content_text.strip():
                logger.warning("Agent produced empty response; retrying once")
                current_messages.append(
                    SystemMessage(
                        content=(
                            "Your previous response was empty. You MUST either:\n"
                            "1) Call the appropriate tool(s), OR\n"
                            "2) Provide a final answer.\n\n"
                            "Do not return an empty message."
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
                    run_id=run_id,
                    owner_id=owner_id,
                    model=model,
                    trace_id=trace_id,
                    enable_token_stream=enable_token_stream,
                )

                # Still empty? Return error message
                if isinstance(llm_response, AIMessage) and not llm_response.tool_calls:
                    retry_text = ""
                    retry_content = llm_response.content
                    if isinstance(retry_content, list):
                        for part in retry_content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                retry_text += str(part.get("text") or "")
                            elif isinstance(part, str):
                                retry_text += part
                    else:
                        retry_text = str(retry_content or "")

                    if not retry_text.strip():
                        logger.error("Agent produced empty response after retry")
                        llm_response = AIMessage(content=("Error: LLM returned an empty response twice. This is a provider/model issue."))

        # Main ReAct loop with iteration guard
        iteration = 0
        while isinstance(llm_response, AIMessage) and llm_response.tool_calls:
            iteration += 1
            if iteration > MAX_REACT_ITERATIONS:
                logger.error(f"ReAct loop exceeded {MAX_REACT_ITERATIONS} iterations. " "Possible infinite loop detected. Returning error.")
                error_msg = AIMessage(
                    content=(
                        f"Error: Supervisor exceeded maximum of {MAX_REACT_ITERATIONS} "
                        "tool iterations. This may indicate a loop or overly complex task."
                    )
                )
                current_messages.append(error_msg)
                return SupervisorResult(
                    messages=current_messages,
                    usage=get_llm_usage(),
                    interrupted=False,
                    interrupt_value=None,
                )

            # Add AIMessage to history
            current_messages.append(llm_response)

            # Execute tools in PARALLEL (non-spawn tools run concurrently,
            # spawn_commiss use two-phase commit for barrier synchronization)
            tool_results, interrupt_value = await _execute_tools_parallel(
                llm_response.tool_calls,
                tools_by_name,
                run_id=run_id,
                owner_id=owner_id,
                tool_getter=get_tool_for_execution if lazy_binder else None,
            )

            # Handle interruption from spawn_commis (barrier pattern)
            if interrupt_value:
                # Non-spawn tool results are included, spawn_commiss trigger barrier
                current_messages.extend(tool_results)
                return SupervisorResult(
                    messages=current_messages,
                    usage=get_llm_usage(),
                    interrupted=True,
                    interrupt_value=interrupt_value,
                )

            # Add tool results to history
            current_messages.extend(tool_results)

            # Rebind tools if search_tools was called (Claude Code pattern)
            _maybe_rebind_after_tool_search(tool_results)

            # Call LLM again
            llm_response = await _call_llm(
                current_messages,
                llm_with_tools,
                phase="tool_iteration",
                run_id=run_id,
                owner_id=owner_id,
                model=model,
                trace_id=trace_id,
                enable_token_stream=enable_token_stream,
            )

        # Add final response
        current_messages.append(llm_response)

        return SupervisorResult(
            messages=current_messages,
            usage=get_llm_usage(),
            interrupted=False,
            interrupt_value=None,
        )

    finally:
        # Clear search context (only needed for lazy loading, but safe to call always)
        if lazy_loading:
            clear_search_context()
