"""Runtime ReAct loop for automation chat and task execution.

This is the live execution path behind ``RuntimeRunner``. It owns:
- tool-bound LLM invocation
- ReAct iteration over tool calls
- usage aggregation
- tool-result persistence helpers like truncation/out-of-band storage

The loop stays runtime-focused. It only preserves the execution harness still
needed by chat threads, task runs, and commis continuations.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING

from zerg.managers.runtime_interrupt import RunnerInterrupted
from zerg.services.dispatch_contract import _apply_dispatch_contract
from zerg.services.dispatch_contract import _classify_dispatch_lane
from zerg.services.openai_client import OpenAIChat
from zerg.tools.result_utils import is_critical_tool_error
from zerg.types.messages import AIMessage
from zerg.types.messages import BaseMessage
from zerg.types.messages import SystemMessage
from zerg.types.messages import ToolMessage

if TYPE_CHECKING:
    from zerg.types.tools import Tool as BaseTool

logger = logging.getLogger(__name__)

_llm_usage_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar("runtime_llm_usage", default=None)
MAX_REACT_ITERATIONS = 50


def _empty_usage() -> dict:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "reasoning_tokens": 0}


def reset_llm_usage() -> None:
    """Reset accumulated LLM usage for a new runtime run."""
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


@dataclass
class RuntimeLoopResult:
    """Result from runtime ReAct loop execution."""

    messages: list[BaseMessage]
    usage: dict = field(default_factory=dict)
    interrupted: bool = False
    interrupt_value: dict | None = None


def _make_llm(
    model: str,
    tools: list[BaseTool],
    *,
    reasoning_effort: str = "none",
    tool_choice: dict | str | bool | None = None,
):
    """Create a tool-bound LLM instance for runtime execution."""
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
    from zerg.models_config import _get_api_key_env_var
    from zerg.models_config import build_openai_compatible_client_kwargs
    from zerg.models_config import get_all_models
    from zerg.models_config import get_model_by_id

    model_config = get_model_by_id(model)
    if not model_config:
        available = [m.id for m in get_all_models()]
        raise ValueError(f"Unknown model: {model}. Available: {available}")

    settings = get_settings()
    provider = model_config.provider
    api_key_env_var = _get_api_key_env_var(model_config)
    api_key = os.getenv(api_key_env_var)

    kwargs: dict = {"model": model, "streaming": settings.llm_token_stream}
    if provider == ModelProvider.ANTHROPIC:
        kwargs["api_key"] = api_key
    else:
        kwargs.update(
            build_openai_compatible_client_kwargs(
                provider=provider,
                api_key=api_key,
                base_url=model_config.base_url,
            )
        )
    if not kwargs["api_key"]:
        raise ValueError(f"{api_key_env_var} not configured but model '{model}' selected")

    capabilities = model_config.capabilities or {}
    if capabilities.get("reasoning", False):
        effort = reasoning_effort
        if effort == "none" and not capabilities.get("reasoningNone", False):
            effort = "low"

        if provider == ModelProvider.OPENROUTER:
            kwargs["extra_body"] = {"reasoning": {"effort": effort}}
        elif provider == ModelProvider.OPENAI:
            kwargs["reasoning_effort"] = effort

    llm = OpenAIChat(**kwargs)
    if tool_choice is None:
        return llm.bind_tools(tools)
    try:
        return llm.bind_tools(tools, tool_choice=tool_choice)
    except TypeError:
        return llm.bind_tools(tools)


async def _emit_heartbeats(cancelled: asyncio.Event, run_id: int | None, owner_id: int | None, phase: str) -> None:
    """Emit heartbeats during long LLM calls."""
    from zerg.events import EventType
    from zerg.events import event_bus

    try:
        while not cancelled.is_set():
            await asyncio.sleep(10)
            if cancelled.is_set():
                break
            if run_id is not None:
                await event_bus.publish(
                    EventType.ASSISTANT_HEARTBEAT,
                    {
                        "event_type": EventType.ASSISTANT_HEARTBEAT,
                        "run_id": run_id,
                        "owner_id": owner_id,
                        "activity": "llm_reasoning",
                        "phase": phase,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )
    except (asyncio.CancelledError, Exception):
        pass


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
    """Call the LLM with audit logging, heartbeats, and usage tracking."""
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
    except Exception as exc:
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
                    error=str(exc),
                )
            except Exception as audit_error:
                logger.warning("Failed to log errored LLM response for audit_id=%s: %s", audit_id, audit_error)
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
    except Exception as audit_error:
        logger.warning("Failed to log successful LLM response for audit_id=%s: %s", audit_id, audit_error)

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


def _extract_text_content(msg: AIMessage) -> str:
    """Extract text content from an assistant message."""
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
    """JSON serializer for datetime values in tool results."""
    from datetime import date as date_type

    if isinstance(obj, (datetime, date_type)):
        return obj.isoformat()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


async def _execute_tool(
    tool_call: dict,
    tools_by_name: dict[str, BaseTool],
    *,
    run_id: int | None,
    owner_id: int | None,
    tool_getter: callable | None = None,
) -> ToolMessage:
    """Execute one tool call, preserving interrupt semantics."""
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

    tool_to_call = tool_getter(tool_name) if tool_getter else tools_by_name.get(tool_name)
    if not tool_to_call:
        result_content = f"Error: Tool '{tool_name}' not found."
        logger.error(result_content)
    else:
        try:
            if getattr(tool_to_call, "coroutine", None):
                observation = await tool_to_call.ainvoke(tool_args)
                if isinstance(observation, dict):
                    result_content = json.dumps(observation, default=_json_default)
                else:
                    result_content = str(observation)
            else:
                observation = await asyncio.to_thread(tool_to_call.invoke, tool_args)
                if isinstance(observation, dict):
                    result_content = json.dumps(observation, default=_json_default)
                else:
                    result_content = str(observation)
        except RunnerInterrupted as exc:
            interrupt_value = exc.interrupt_value
            if tool_call_id:
                if isinstance(interrupt_value, dict):
                    interrupt_value = {
                        **interrupt_value,
                        "tool_call_id": interrupt_value.get("tool_call_id") or tool_call_id,
                    }
                else:
                    interrupt_value = {"tool_call_id": tool_call_id}
            raise RunnerInterrupted(interrupt_value or {})
        except Exception as exc:
            result_content = f"<tool-error> {exc}"
            logger.exception("Error executing tool %s", tool_name)

    duration_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)
    if result_content is None:
        result_content = "(No result)"

    raw_result = str(result_content)
    is_error, error_msg = check_tool_error(raw_result)

    if is_error and is_critical_tool_error(raw_result, error_msg, tool_name=tool_name):
        if emitter and hasattr(emitter, "mark_critical_error"):
            emitter.mark_critical_error(error_msg or raw_result)
        logger.warning("Critical tool error in %s: %s", tool_name, error_msg or raw_result)

    result_content = _maybe_truncate_result(raw_result, tool_name, run_id, owner_id, tool_call_id)

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


def _maybe_truncate_result(
    raw: str,
    tool_name: str,
    run_id: int | None,
    owner_id: int | None,
    tool_call_id: str,
) -> str:
    """Store large tool outputs out of band and return a preview."""
    from zerg.config import get_settings
    from zerg.services.tool_output_store import ToolOutputStore

    settings = get_settings()
    max_chars = max(0, int(settings.tool_output_max_chars or 0))
    if max_chars <= 0 or len(raw) <= max_chars or tool_name == "get_tool_output":
        return raw

    preview_chars = max(0, int(settings.tool_output_preview_chars or 0))
    preview_chars = min(preview_chars, max_chars) if preview_chars > 0 else min(200, max_chars)
    preview = raw[:preview_chars]

    if owner_id is None:
        return (
            f"(Tool output truncated; exceeded {max_chars} characters.)\n"
            "Full output was not stored (no owner_id).\n"
            f"Preview:\n{preview}"
        )

    try:
        store = ToolOutputStore()
        artifact_id = store.save_output(
            owner_id=owner_id,
            tool_name=tool_name,
            content=raw,
            run_id=run_id,
            tool_call_id=tool_call_id,
        )
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
            f"(Tool output truncated; exceeded {max_chars} characters.)\n"
            "Full output was not stored (storage failed).\n"
            f"Preview:\n{preview}"
        )


async def _execute_tools_parallel(
    tool_calls: list[dict],
    tools_by_name: dict[str, BaseTool],
    *,
    run_id: int | None,
    owner_id: int | None,
    tool_getter: callable | None = None,
) -> tuple[list[ToolMessage], dict | None]:
    """Execute multiple tools in parallel."""
    tool_results: list[ToolMessage] = []

    async def _exec(tc: dict) -> ToolMessage:
        try:
            return await _execute_tool(tc, tools_by_name, run_id=run_id, owner_id=owner_id, tool_getter=tool_getter)
        except RunnerInterrupted:
            raise
        except Exception as exc:
            logger.exception("Parallel tool error: %s", tc.get("name"))
            return ToolMessage(
                content=f"<tool-error>{exc}</tool-error>",
                tool_call_id=tc.get("id", ""),
                name=tc.get("name", "unknown"),
            )

    results = await asyncio.gather(*[_exec(tc) for tc in tool_calls], return_exceptions=True)
    for tc, result in zip(tool_calls, results):
        if isinstance(result, RunnerInterrupted):
            raise result
        if isinstance(result, Exception):
            tool_results.append(
                ToolMessage(
                    content=f"<tool-error>{result}</tool-error>",
                    tool_call_id=tc.get("id", ""),
                    name=tc.get("name", "unknown"),
                )
            )
        else:
            tool_results.append(result)

    return tool_results, None


async def run_runtime_react_loop(
    messages: list[BaseMessage],
    fiche_row=None,
    tools: list[BaseTool] | None = None,
    *,
    run_id: int | None = None,
    owner_id: int | None = None,
    trace_id: str | None = None,
    enable_token_stream: bool = False,
    lazy_loading: bool = False,
) -> RuntimeLoopResult:
    """Run the runtime ReAct loop until completion or interrupt."""
    if tools is None:
        tools = []

    if lazy_loading:
        from zerg.tools import get_registry
        from zerg.tools.lazy_binder import LazyToolBinder
        from zerg.tools.tool_search import build_catalog
        from zerg.tools.tool_search import clear_search_context
        from zerg.tools.tool_search import format_catalog_for_prompt
        from zerg.tools.tool_search import set_search_context

        registry = get_registry()
        allowed_names = [tool.name for tool in tools]
        lazy_binder = LazyToolBinder(registry, allowed_tools=allowed_names)
        set_search_context(allowed_tools=allowed_names, max_results=8)

        bound_tools = lazy_binder.get_bound_tools()
        tools_by_name = {tool.name: tool for tool in tools}

        loaded_core_names = sorted(lazy_binder.loaded_tool_names)
        catalog = build_catalog()
        catalog_text = format_catalog_for_prompt(catalog, exclude_core=True)
        instructions = "You have access to the following tools. Core tools are always available."
        if "search_tools" in loaded_core_names:
            instructions += " For other tools, first call `search_tools` with a query describing what you need."
        catalog_header = f"\n\n## Available Tools\n{instructions}\n"
        catalog_header += f"\n### Core Tools (always loaded): {', '.join(loaded_core_names)}\n{catalog_text}"
        if messages and hasattr(messages[0], "type") and messages[0].type == "system":
            messages = [SystemMessage(content=messages[0].content + catalog_header)] + list(messages[1:])

        logger.info("[LazyLoading] %s core tools, %s total, catalog injected", len(bound_tools), len(tools))
    else:
        lazy_binder = None
        bound_tools = tools
        tools_by_name = {tool.name: tool for tool in tools}

    model = fiche_row.model
    cfg = getattr(fiche_row, "config", {}) or {}
    reasoning_effort = (cfg.get("reasoning_effort") or "none").lower()
    reset_llm_usage()

    try:
        llm_with_tools = _make_llm(model=model, tools=bound_tools, reasoning_effort=reasoning_effort)
        current_messages = list(messages)

        def get_tool_for_execution(tool_name: str) -> BaseTool | None:
            nonlocal llm_with_tools, bound_tools
            if not lazy_binder:
                return tools_by_name.get(tool_name)
            tool = lazy_binder.get_tool(tool_name)
            if lazy_binder.needs_rebind():
                bound_tools = lazy_binder.get_bound_tools()
                llm_with_tools = _make_llm(model=model, tools=bound_tools, reasoning_effort=reasoning_effort)
                lazy_binder.clear_rebind_flag()
                logger.info("[LazyLoading] Rebound LLM with %s tools after loading '%s'", len(bound_tools), tool_name)
            return tool

        max_tools_from_search = 8

        def _maybe_rebind_after_tool_search(tool_results: list[ToolMessage]) -> None:
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
            deduped = [name for name in names if not (name in seen or seen.add(name))][:max_tools_from_search]
            loaded = lazy_binder.load_tools(deduped)
            if lazy_binder.needs_rebind():
                bound_tools = lazy_binder.get_bound_tools()
                llm_with_tools = _make_llm(model=model, tools=bound_tools, reasoning_effort=reasoning_effort)
                lazy_binder.clear_rebind_flag()
                logger.info("[LazyLoading] Rebound after search_tools; loaded=%s, total=%s", loaded, len(bound_tools))

        llm_kwargs = dict(
            run_id=run_id,
            owner_id=owner_id,
            model=model,
            trace_id=trace_id,
            enable_token_stream=enable_token_stream,
        )

        phase = "initial"
        if current_messages:
            last_msg = current_messages[-1]
            if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
                pending_ids = {tc["id"] for tc in last_msg.tool_calls}
                responded_ids = {m.tool_call_id for m in current_messages if isinstance(m, ToolMessage)}
                unresponded = pending_ids - responded_ids

                if unresponded:
                    logger.info("Resuming with %s pending tool call(s)", len(unresponded))
                    pending_calls = [tc for tc in last_msg.tool_calls if tc["id"] in unresponded]
                    pending_calls = _apply_dispatch_contract(pending_calls, current_messages) or pending_calls
                    tool_results, interrupt_value = await _execute_tools_parallel(
                        pending_calls,
                        tools_by_name,
                        run_id=run_id,
                        owner_id=owner_id,
                        tool_getter=get_tool_for_execution if lazy_binder else None,
                    )
                    if interrupt_value:
                        current_messages.extend(tool_results)
                        return RuntimeLoopResult(
                            messages=current_messages,
                            usage=get_llm_usage(),
                            interrupted=True,
                            interrupt_value=interrupt_value,
                        )
                    current_messages.extend(tool_results)
                    _maybe_rebind_after_tool_search(tool_results)
                    phase = "resume_synthesis"

        llm_response = await _call_llm(current_messages, llm_with_tools, phase=phase, **llm_kwargs)

        response_is_empty = (
            isinstance(llm_response, AIMessage) and not llm_response.tool_calls and not _extract_text_content(llm_response).strip()
        )
        if response_is_empty:
            logger.warning("Runtime loop produced empty response; retrying once")
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
            retry_is_empty = (
                isinstance(llm_response, AIMessage) and not llm_response.tool_calls and not _extract_text_content(llm_response).strip()
            )
            if retry_is_empty:
                logger.error("Runtime loop produced empty response after retry")
                llm_response = AIMessage(content="Error: LLM returned an empty response twice. This is a provider/model issue.")

        if isinstance(llm_response, AIMessage):
            llm_response.tool_calls = _apply_dispatch_contract(llm_response.tool_calls, current_messages)
            dispatch_lane = _classify_dispatch_lane(llm_response.tool_calls)
            tool_count = len(llm_response.tool_calls or [])
            logger.debug("[DispatchContract] lane=%s tool_calls=%s", dispatch_lane, tool_count)

        iteration = 0
        while isinstance(llm_response, AIMessage) and llm_response.tool_calls:
            iteration += 1
            if iteration > MAX_REACT_ITERATIONS:
                logger.error("Runtime loop exceeded %s iterations", MAX_REACT_ITERATIONS)
                current_messages.append(
                    AIMessage(content=f"Error: Runtime loop exceeded maximum of {MAX_REACT_ITERATIONS} tool iterations.")
                )
                return RuntimeLoopResult(messages=current_messages, usage=get_llm_usage())

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
                return RuntimeLoopResult(
                    messages=current_messages,
                    usage=get_llm_usage(),
                    interrupted=True,
                    interrupt_value=interrupt_value,
                )

            current_messages.extend(tool_results)
            _maybe_rebind_after_tool_search(tool_results)

            llm_response = await _call_llm(current_messages, llm_with_tools, phase="tool_iteration", **llm_kwargs)
            if isinstance(llm_response, AIMessage):
                llm_response.tool_calls = _apply_dispatch_contract(llm_response.tool_calls, current_messages)
                dispatch_lane = _classify_dispatch_lane(llm_response.tool_calls)
                tool_count = len(llm_response.tool_calls or [])
                logger.debug("[DispatchContract] lane=%s tool_calls=%s", dispatch_lane, tool_count)

        current_messages.append(llm_response)
        return RuntimeLoopResult(messages=current_messages, usage=get_llm_usage())
    finally:
        if lazy_loading:
            clear_search_context()
