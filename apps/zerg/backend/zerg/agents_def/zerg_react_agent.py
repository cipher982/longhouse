"""Pure agent definition using LangGraph Functional API (ReAct style).

This module contains **no database logic** it is purely responsible for
defining *how the agent thinks*.  Persistence and streaming will be handled by
AgentRunner.
"""

import contextvars
import logging
from typing import Dict
from typing import List
from typing import Optional

from langchain_core.messages import AIMessage
from langchain_core.messages import BaseMessage
from langchain_core.messages import ToolMessage

# External dependencies
from langchain_openai import ChatOpenAI
from langgraph.func import entrypoint
from langgraph.graph.message import add_messages

# Local imports (late to avoid circulars)
from zerg.config import get_settings

# Worker context for tool event emission
from zerg.context import get_worker_context

# Centralised flags
from zerg.tools.unified_access import get_tool_resolver

logger = logging.getLogger(__name__)

# Track run_ids that have already been warned about evidence mounting issues
# Prevents log spam since _call_model_async is called multiple times per ReAct loop
# NOTE: Call clear_evidence_mount_warning(run_id) when run completes to prevent unbounded growth
_evidence_mount_warned_runs: set[int] = set()


def clear_evidence_mount_warning(run_id: int) -> None:
    """Clear the warning-once state for a completed supervisor run.

    Call this when a supervisor run completes to prevent unbounded memory growth
    in long-running servers.
    """
    _evidence_mount_warned_runs.discard(run_id)


# Context variable to store accumulated LLM usage data (set during LLM calls, read by AgentRunner)
# This is needed because LangGraph streaming doesn't preserve usage metadata on AIMessage objects
_llm_usage_var: contextvars.ContextVar[Dict] = contextvars.ContextVar("llm_usage", default={})

# Context variable to store pending AIMessage that needs to be persisted on interrupt
# When LLM returns tool_calls, we store the AIMessage here before executing tools.
# If interrupt() is called during tool execution, agent_runner reads this and persists it.
_pending_ai_message_var: contextvars.ContextVar[Optional[AIMessage]] = contextvars.ContextVar("pending_ai_message", default=None)


def set_pending_ai_message(msg: AIMessage) -> None:
    """Store an AIMessage that should be persisted if interrupt occurs."""
    _pending_ai_message_var.set(msg)


def get_pending_ai_message() -> Optional[AIMessage]:
    """Get the pending AIMessage (for agent_runner to persist on interrupt)."""
    return _pending_ai_message_var.get()


def clear_pending_ai_message() -> None:
    """Clear the pending AIMessage (after tools complete or after persisting)."""
    _pending_ai_message_var.set(None)


def reset_llm_usage() -> None:
    """Reset the accumulated LLM usage data. Call before starting a new agent run."""
    _llm_usage_var.set({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "reasoning_tokens": 0})


# LLM request logging - captures full payloads for debugging
_LLM_REQUEST_LOG_ENABLED = True  # Toggle for performance


def _log_llm_request(messages: List, model: str, phase: str, worker_id: str | None = None) -> None:
    """Log the full LLM request payload for debugging.

    Writes to data/llm_requests/ directory with structured JSON.
    """
    if not _LLM_REQUEST_LOG_ENABLED:
        return

    import json
    from datetime import datetime
    from pathlib import Path

    try:
        # Create log directory
        log_dir = Path("data/llm_requests")
        log_dir.mkdir(parents=True, exist_ok=True)

        # Build structured log entry
        timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "model": model,
            "phase": phase,
            "worker_id": worker_id,
            "message_count": len(messages),
            "messages": [],
        }

        # Convert messages to serializable format
        for i, msg in enumerate(messages):
            msg_dict = {
                "index": i,
                "type": type(msg).__name__,
                "role": getattr(msg, "type", "unknown"),
            }

            # Get content
            content = getattr(msg, "content", None)
            if isinstance(content, str):
                msg_dict["content"] = content
                msg_dict["content_length"] = len(content)
            elif isinstance(content, list):
                msg_dict["content"] = content
                msg_dict["content_length"] = sum(len(str(c)) for c in content)
            else:
                msg_dict["content"] = str(content) if content else None

            # Get tool calls if present
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                msg_dict["tool_calls"] = tool_calls

            # Get tool_call_id if present (for ToolMessage)
            tool_call_id = getattr(msg, "tool_call_id", None)
            if tool_call_id:
                msg_dict["tool_call_id"] = tool_call_id

            # Get name if present (for ToolMessage)
            name = getattr(msg, "name", None)
            if name:
                msg_dict["name"] = name

            log_entry["messages"].append(msg_dict)

        # Write to file
        worker_suffix = f"_{worker_id[:30]}" if worker_id else ""
        filename = f"{timestamp}_{phase}{worker_suffix}.json"
        filepath = log_dir / filename

        with open(filepath, "w") as f:
            json.dump(log_entry, f, indent=2, default=str)

        logger.debug(f"[LLM_REQUEST] Logged to {filepath}")

    except Exception as e:
        logger.warning(f"Failed to log LLM request: {e}")


def _log_llm_response(result, model: str, phase: str, duration_ms: int, worker_id: str | None = None) -> None:
    """Log the LLM response for debugging.

    Writes to data/llm_requests/ directory with structured JSON.
    """
    if not _LLM_REQUEST_LOG_ENABLED:
        return

    import json
    from datetime import datetime
    from pathlib import Path

    try:
        log_dir = Path("data/llm_requests")
        log_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "type": "response",
            "model": model,
            "phase": phase,
            "worker_id": worker_id,
            "duration_ms": duration_ms,
            "response": {},
        }

        # Extract response details
        if hasattr(result, "content"):
            content = result.content
            if isinstance(content, str):
                log_entry["response"]["content"] = content
                log_entry["response"]["content_length"] = len(content)
            elif isinstance(content, list):
                log_entry["response"]["content"] = content
            else:
                log_entry["response"]["content"] = str(content) if content else None

        if hasattr(result, "tool_calls") and result.tool_calls:
            log_entry["response"]["tool_calls"] = result.tool_calls

        if hasattr(result, "usage_metadata"):
            log_entry["response"]["usage_metadata"] = result.usage_metadata

        # Write to file
        worker_suffix = f"_{worker_id[:30]}" if worker_id else ""
        filename = f"{timestamp}_{phase}_response{worker_suffix}.json"
        filepath = log_dir / filename

        with open(filepath, "w") as f:
            json.dump(log_entry, f, indent=2, default=str)

        logger.debug(f"[LLM_RESPONSE] Logged to {filepath}")

    except Exception as e:
        logger.warning(f"Failed to log LLM response: {e}")


def get_llm_usage() -> Dict:
    """Get the accumulated LLM usage data from the current run."""
    return _llm_usage_var.get()


def _accumulate_llm_usage(usage: Dict) -> None:
    """Add usage data from an LLM call to the accumulated total."""
    current = _llm_usage_var.get()
    if not current:
        current = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "reasoning_tokens": 0}

    current["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
    current["completion_tokens"] += usage.get("completion_tokens", 0) or 0
    current["total_tokens"] += usage.get("total_tokens", 0) or 0

    # Extract reasoning_tokens from completion_tokens_details
    details = usage.get("completion_tokens_details") or {}
    current["reasoning_tokens"] += details.get("reasoning_tokens", 0) or 0

    _llm_usage_var.set(current)


def is_critical_tool_error(
    result_content: str,
    error_msg: str | None,
    *,
    tool_name: str | None = None,
) -> bool:
    """Return True if a tool error is a "fail-fast" configuration/setup problem.

    This intentionally errs on the side of *not* failing fast so the agent can:
    - correct bad arguments (validation errors)
    - try alternate tools (e.g. runner_exec -> ssh_exec)
    """

    # runner_exec has a natural fallback (ssh_exec) for single-user/dev setups.
    # Treat runner failures as non-critical so the model can attempt alternatives.
    if tool_name == "runner_exec":
        return False

    content_lower = (result_content or "").lower()
    msg_lower = (error_msg or "").lower()
    combined = f"{content_lower} {msg_lower}"

    # Connector/configuration errors (typically non-recoverable in-task)
    config_indicators = [
        "connector_not_configured",
        "not configured",
        "not connected",
        "invalid_credentials",
        "credentials have expired",
        "ssh client not found",
        "ssh key not found",
        "no ssh key",
        "not found in path",
    ]
    if any(indicator in combined for indicator in config_indicators):
        return True

    # Permission errors usually indicate missing key/rights.
    # (This can be "wrong user", but failing fast tends to be preferable here.)
    if "permission_denied" in combined or "permission denied" in combined:
        return True

    # Execution errors that strongly indicate infra/setup issues (SSH/host/network)
    if "execution_error" in combined and any(term in combined for term in ["ssh", "connection", "host", "unreachable"]):
        return True

    # Validation errors are usually recoverable (bad args/format); don't fail fast.
    if "validation_error" in combined:
        return False

    # Transient failures (timeouts, rate limits) are non-critical.
    transient_indicators = [
        "timeout",
        "timed out",
        "rate_limited",
        "rate limit",
        "temporarily unavailable",
    ]
    if any(indicator in combined for indicator in transient_indicators):
        return False

    return False


# ---------------------------------------------------------------------------
# LLM Factory (remains similar, adjusted docstring/comment)
# ---------------------------------------------------------------------------


def _make_llm(agent_row, tools, *, tool_choice: dict | str | bool | None = None):
    """Factory that returns a *tool-bound* ``ChatOpenAI`` instance.

    If the :pydataattr:`zerg.config.LLM_TOKEN_STREAM` flag is enabled the LLM
    will be configured for *streaming* and the ``WsTokenCallback`` will be
    attached so each new token is forwarded to the WebSocket layer.
    """

    # Feature flag – evaluate the environment variable *lazily* so test cases
    # that tweak ``LLM_TOKEN_STREAM`` via ``monkeypatch.setenv`` after the
    # module import still take effect.

    from zerg.testing.test_models import is_test_model
    from zerg.testing.test_models import require_testing_mode

    settings = get_settings()
    enable_token_stream = settings.llm_token_stream

    # Handle mock/scripted models for testing (requires TESTING=1)
    if is_test_model(agent_row.model):
        require_testing_mode(agent_row.model, settings)

        if agent_row.model == "gpt-mock":
            from zerg.testing.mock_llm import MockChatLLM

            llm = MockChatLLM()
            try:
                return llm.bind_tools(tools, tool_choice=tool_choice)
            except TypeError:
                return llm.bind_tools(tools)

    # Create LLM with basic parameters
    kwargs: dict = {
        "model": agent_row.model,
        "streaming": enable_token_stream,
        "api_key": get_settings().openai_api_key,
    }

    # Add reasoning_effort - always pass explicitly, default to "none"
    # Values: "none", "low", "medium", "high", "xhigh"
    agent_cfg = getattr(agent_row, "config", {}) or {}
    reasoning_effort = (agent_cfg.get("reasoning_effort") or "none").lower()
    kwargs["reasoning_effort"] = reasoning_effort

    logger.info(f"[_make_llm] Creating LLM with model={agent_row.model}, reasoning_effort={reasoning_effort}")

    llm = ChatOpenAI(**kwargs)

    # Note: callbacks should be passed during invocation, not construction
    # The WsTokenCallback should be handled at the invocation level

    if tool_choice is None:
        return llm.bind_tools(tools)

    try:
        return llm.bind_tools(tools, tool_choice=tool_choice)
    except TypeError:
        # Some stubs / older LangChain implementations don't accept tool_choice.
        return llm.bind_tools(tools)


# ---------------------------------------------------------------------------
# Main Agent Implementation
# ---------------------------------------------------------------------------


def get_runnable(agent_row):  # noqa: D401 – matches public API naming
    """
    Return a compiled LangGraph runnable using the Functional API
    for the given Agent ORM row.
    """
    # NOTE: Do NOT capture token stream setting here – it must be evaluated
    # at invocation time, not at runnable creation time. This allows the
    # LLM_TOKEN_STREAM environment variable to be changed without restarting.

    # --- Define tools and model within scope ---
    # ------------------------------------------------------------------
    # MCP INTEGRATION – Dynamically load tools provided by *all* MCP servers
    # referenced in the agent configuration.  We run the **synchronous**
    # helper which internally spins up an event-loop and fetches the tool
    # manifests.  Duplicate servers across multiple agents are cached by the
    # ``MCPManager`` so each server is contacted at most once per process.
    # ------------------------------------------------------------------

    cfg = getattr(agent_row, "config", {}) or {}
    if "mcp_servers" in cfg:
        # Deferred import to avoid cost when MCP is unused
        from zerg.tools.mcp_adapter import load_mcp_tools_sync  # noqa: WPS433 (late import)

        load_mcp_tools_sync(cfg["mcp_servers"])  # blocking – runs quickly (metadata only)

    # ------------------------------------------------------------------
    # Tool resolution using unified access
    # ------------------------------------------------------------------
    resolver = get_tool_resolver()
    allowed_tools = getattr(agent_row, "allowed_tools", None)
    tools = resolver.filter_by_allowlist(allowed_tools)

    if not tools:
        logger.warning(f"No tools available for agent {agent_row.id}")

    tools_by_name = {tool.name: tool for tool in tools}
    # NOTE: DO NOT create llm_with_tools here - it must be created at invocation time
    # to respect the enable_token_stream flag which can change at runtime

    # ------------------------------------------------------------------
    # CHECKPOINTER SELECTION – Production vs Test/Dev
    # ------------------------------------------------------------------
    # Use PostgresSaver for production (durable checkpoints that survive restarts)
    # Use MemorySaver for SQLite/tests (fast in-memory checkpoints)
    # The factory inspects the database URL and returns the appropriate implementation.
    # ------------------------------------------------------------------
    from zerg.services.checkpointer import get_checkpointer

    checkpointer = get_checkpointer()

    # --- Define Tasks ---
    # ------------------------------------------------------------------
    # Model invocation helpers
    # ------------------------------------------------------------------

    def _call_model_sync(
        messages: List[BaseMessage],
        enable_token_stream: bool = False,
        *,
        tool_choice: dict | str | bool | None = None,
    ):
        """Blocking LLM call (executes in *current* thread).

        We keep this as a *plain* function rather than a LangGraph ``@task``
        because the latter returns a *Task* object that requires calling
        ``.result()`` **inside** a runnable context.  Our agent executes the
        model call from within its own coroutine, *outside* the graph
        execution engine, therefore the additional indirection only made the
        code harder to reason about and raised confusing runtime errors like:

            "Called get_config outside of a runnable context"
        """
        # Create LLM dynamically to respect current enable_token_stream flag
        if tool_choice is None:
            llm_with_tools = _make_llm(agent_row, tools)
        else:
            llm_with_tools = _make_llm(agent_row, tools, tool_choice=tool_choice)
        return llm_with_tools.invoke(messages)

    async def _call_model_async(
        messages: List[BaseMessage],
        enable_token_stream: bool = False,
        phase: str = "reasoning",
        *,
        tool_choice: dict | str | bool | None = None,
    ):
        """Run the LLM call with optional token streaming via callbacks.

        Parameters
        ----------
        messages
            Message history to send to the LLM
        enable_token_stream
            Whether to enable token streaming
        phase
            Phase name for metrics tracking (e.g., "reasoning", "synthesis")
        """
        import asyncio
        from datetime import datetime
        from datetime import timezone

        # Track timing for metrics
        start_time = datetime.now(timezone.utc)

        # Phase 6: Start heartbeat task to signal progress during long LLM calls
        # This prevents roundabout from thinking the worker is stuck during reasoning
        heartbeat_task = None
        heartbeat_cancelled = asyncio.Event()

        async def emit_heartbeats():
            """Emit heartbeats every 10 seconds during LLM call."""
            from zerg.context import get_worker_context
            from zerg.events import EventType
            from zerg.events import event_bus
            from zerg.services.supervisor_context import get_supervisor_context

            ctx = get_worker_context()
            sup_ctx = get_supervisor_context()
            supervisor_run_id = sup_ctx.run_id if sup_ctx else None

            try:
                while not heartbeat_cancelled.is_set():
                    await asyncio.sleep(10)  # Wait 10 seconds between heartbeats
                    if heartbeat_cancelled.is_set():
                        break

                    if ctx:
                        await event_bus.publish(
                            EventType.WORKER_HEARTBEAT,
                            {
                                "event_type": EventType.WORKER_HEARTBEAT,
                                "worker_id": ctx.worker_id,
                                "owner_id": ctx.owner_id,
                                "run_id": ctx.run_id,
                                "job_id": ctx.job_id,
                                "activity": "llm_reasoning",
                                "phase": phase,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                        )
                        logger.debug(f"Emitted heartbeat for worker {ctx.worker_id} during {phase}")
                    elif supervisor_run_id:
                        await event_bus.publish(
                            EventType.SUPERVISOR_HEARTBEAT,
                            {
                                "event_type": EventType.SUPERVISOR_HEARTBEAT,
                                "run_id": supervisor_run_id,
                                "owner_id": agent_row.owner_id,
                                "activity": "llm_reasoning",
                                "phase": phase,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                        )
                        logger.debug(f"Emitted heartbeat for supervisor run {supervisor_run_id} during {phase}")
            except asyncio.CancelledError:
                pass  # Normal shutdown
            except Exception as e:
                logger.warning(f"Error in heartbeat task: {e}")

        # Start heartbeat task in background
        heartbeat_task = asyncio.create_task(emit_heartbeats())

        # Create LLM dynamically with current enable_token_stream flag
        if tool_choice is None:
            llm_with_tools = _make_llm(agent_row, tools)
        else:
            llm_with_tools = _make_llm(agent_row, tools, tool_choice=tool_choice)

        # Log the full request payload for debugging
        # Import explicitly to avoid scoping issues with nested function
        from zerg.context import get_worker_context as _get_ctx

        _ctx = _get_ctx()
        _worker_id = _ctx.worker_id if _ctx else None
        _log_llm_request(messages, agent_row.model, phase, _worker_id)

        # Phase 2: Evidence Mounting for Supervisor Runs
        # If we're in a supervisor context, wrap the LLM with evidence mounting
        from zerg.services.supervisor_context import get_supervisor_context

        _sup_ctx = get_supervisor_context()
        supervisor_run_id = _sup_ctx.run_id if _sup_ctx else None
        if supervisor_run_id is not None:
            # We're in a supervisor run - wrap LLM for evidence mounting
            from zerg.services.evidence_mounting_llm import EvidenceMountingLLM

            # Helper to warn once per run_id (prevents log spam in ReAct loop)
            def _warn_once(msg: str) -> None:
                if supervisor_run_id not in _evidence_mount_warned_runs:
                    _evidence_mount_warned_runs.add(supervisor_run_id)
                    logger.warning(msg)

            # Get database session from credential resolver context
            try:
                from zerg.connectors.context import get_credential_resolver

                resolver = get_credential_resolver()
                if resolver is None:
                    _warn_once(
                        f"Evidence mounting disabled for supervisor run_id={supervisor_run_id}: "
                        "CredentialResolver context not available. Evidence from worker tools will not be mounted."
                    )
                else:
                    db = resolver.db  # CredentialResolver has db attribute
                    owner_id = getattr(agent_row, "owner_id", None)
                    if owner_id is None:
                        _warn_once(
                            f"Evidence mounting disabled for supervisor run_id={supervisor_run_id}: " "owner_id not available on agent_row"
                        )
                    elif db is None:
                        _warn_once(
                            f"Evidence mounting disabled for supervisor run_id={supervisor_run_id}: "
                            "Database session not available in CredentialResolver"
                        )
                    else:
                        llm_with_tools = EvidenceMountingLLM(
                            base_llm=llm_with_tools,
                            run_id=supervisor_run_id,
                            owner_id=owner_id,
                            db=db,
                        )
                        # Only log on first LLM call of this run (when we actually wrap)
                        if supervisor_run_id not in _evidence_mount_warned_runs:
                            logger.debug(f"Evidence mounting enabled for supervisor run_id={supervisor_run_id}, owner_id={owner_id}")
            except Exception as e:
                # Evidence mounting is best-effort - don't fail if context is unavailable
                _warn_once(f"Evidence mounting disabled for supervisor run_id={supervisor_run_id}: {e}")

        try:
            if enable_token_stream:
                from zerg.callbacks.token_stream import WsTokenCallback

                callback = WsTokenCallback()
                # Pass callbacks via config - LangChain will call on_llm_new_token during streaming
                # With langchain-core 1.2.5+, usage_metadata is populated on the result
                result = await llm_with_tools.ainvoke(messages, config={"callbacks": [callback]})
            else:
                # CRITICAL: Always use the wrapped LLM (with evidence mounting) even when not streaming
                # Using _call_model_sync would bypass the EvidenceMountingLLM wrapper
                result = await llm_with_tools.ainvoke(messages)
        finally:
            # Phase 6: Stop heartbeat task after LLM call completes
            heartbeat_cancelled.set()
            if heartbeat_task:
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

        # Log the response for debugging
        _log_llm_response(result, agent_row.model, phase, duration_ms, _worker_id)

        # Extract and accumulate token usage (always, for AgentRunner)
        # With langchain-core 1.2.5+, usage_metadata is the canonical location for usage
        if isinstance(result, AIMessage):
            usage_meta = getattr(result, "usage_metadata", None)

            logger.info(f"[_call_model_async] usage_metadata: {usage_meta}")

            # Accumulate usage in context variable (for AgentRunner to retrieve)
            if usage_meta:
                # Convert from usage_metadata format to our internal format
                usage_dict = {
                    "prompt_tokens": usage_meta.get("input_tokens", 0),
                    "completion_tokens": usage_meta.get("output_tokens", 0),
                    "total_tokens": usage_meta.get("total_tokens", 0),
                    "completion_tokens_details": {"reasoning_tokens": usage_meta.get("output_token_details", {}).get("reasoning", 0)},
                }
                _accumulate_llm_usage(usage_dict)

        # Record metrics if collector is available (workers only)
        from zerg.worker_metrics import get_metrics_collector

        collector = get_metrics_collector()
        if collector:
            # Extract token usage from result for metrics (use usage_metadata)
            prompt_tokens = None
            completion_tokens = None
            total_tokens = None

            if isinstance(result, AIMessage):
                usage_meta = getattr(result, "usage_metadata", None)
                if usage_meta:
                    prompt_tokens = usage_meta.get("input_tokens")
                    completion_tokens = usage_meta.get("output_tokens")
                    total_tokens = usage_meta.get("total_tokens")

            collector.record_llm_call(
                phase=phase,
                model=agent_row.model,
                start_ts=start_time,
                end_ts=end_time,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )

            # Tier 3 telemetry: Structured logging for dev visibility (opaque to LLMs)
            # This provides real-time grep-able logs alongside metrics.jsonl for monitoring/debugging
            try:
                from zerg.context import get_worker_context

                ctx = get_worker_context()
                log_extra = {
                    "phase": phase,
                    "model": agent_row.model,
                    "duration_ms": duration_ms,
                }
                if ctx:
                    log_extra["worker_id"] = ctx.worker_id
                if prompt_tokens is not None:
                    log_extra["prompt_tokens"] = prompt_tokens
                if completion_tokens is not None:
                    log_extra["completion_tokens"] = completion_tokens
                if total_tokens is not None:
                    log_extra["total_tokens"] = total_tokens

                logger.info("llm_call_complete", extra=log_extra)
            except Exception:
                # Telemetry logging is best-effort - don't fail the worker
                pass

        return result

    #
    # NOTE ON CONCURRENCY
    # -------------------
    # The previous implementation claimed to run tool calls in *parallel* but
    # still resolved each future via ``future.result()`` **one-by-one** which
    # effectively serialised the loop once the first blocking call was hit.
    #
    # We now expose *both* a classic **sync** task wrapper (kept for
    # backwards-compatibility with the LangGraph Functional API runner) **and**
    # a thin **async** helper that executes the synchronous wrapper via
    # ``asyncio.to_thread`` so that callers can await *all* tool calls using
    # ``asyncio.gather``.
    #

    def _call_tool_sync(tool_call: dict):  # noqa: D401 – internal helper
        """Execute a single tool call (blocking)."""
        import json

        from langgraph.errors import GraphInterrupt

        tool_name = tool_call["name"]
        tool_args = tool_call.get("args", {})
        tool_call_id = tool_call["id"]

        tool_to_call = tools_by_name.get(tool_name)

        if not tool_to_call:
            observation = f"Error: Tool '{tool_name}' not found."
            logger.error(observation)
        else:
            try:
                observation = tool_to_call.invoke(tool_args)
            except GraphInterrupt:
                # Let interrupt bubble up to pause the graph (spawn_worker uses this)
                raise
            except Exception as exc:  # noqa: BLE001
                observation = f"<tool-error> {exc}"
                logger.exception("Error executing tool %s", tool_name)

        # Serialize observation to JSON if it's a dict (tool_success/tool_error envelope)
        # This ensures consistent JSON format for evidence parsing and artifact storage
        if isinstance(observation, dict):
            from datetime import date
            from datetime import datetime

            def datetime_handler(obj):
                if isinstance(obj, (datetime, date)):
                    return obj.isoformat()
                raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")

            content = json.dumps(observation, default=datetime_handler)
        else:
            content = str(observation)

        return ToolMessage(content=content, tool_call_id=tool_call_id, name=tool_name)

    # ---------------------------------------------------------------
    # Import helper functions for tool result processing
    # ---------------------------------------------------------------
    from zerg.tools.result_utils import check_tool_error
    from zerg.tools.result_utils import redact_sensitive_args
    from zerg.tools.result_utils import safe_preview

    def _format_critical_error(tool_name: str, error_content: str) -> str:
        """Format a critical error message for the worker result.

        Extracts the user-facing message and provides actionable guidance.

        Args:
            tool_name: Name of the tool that failed
            error_content: Error content from the tool

        Returns:
            Human-readable error message with guidance
        """
        # Try to extract user_message from error envelope
        if "user_message" in error_content:
            # Parse error envelope
            try:
                import ast
                import json

                parsed = None
                try:
                    parsed = json.loads(error_content)
                except (json.JSONDecodeError, TypeError):
                    try:
                        parsed = ast.literal_eval(error_content)
                    except (ValueError, SyntaxError):
                        pass

                if isinstance(parsed, dict) and parsed.get("user_message"):
                    user_msg = parsed["user_message"]
                    return f"Tool '{tool_name}' failed: {user_msg}"
            except Exception:
                pass  # Fall through to default formatting

        # Extract the most relevant part of the error
        # Remove <tool-error> prefix if present
        error_clean = error_content.replace("<tool-error>", "").strip()

        # Limit length
        if len(error_clean) > 300:
            error_clean = error_clean[:297] + "..."

        return f"Tool '{tool_name}' failed: {error_clean}"

    async def _call_tool_async(tool_call: dict):  # noqa: D401 – coroutine helper
        """Run tool execution in a worker thread with event emission.

        Emits tool lifecycle events for real-time monitoring:
        - In worker context: WORKER_TOOL_STARTED, WORKER_TOOL_COMPLETED, WORKER_TOOL_FAILED
        - In supervisor context: SUPERVISOR_TOOL_STARTED, SUPERVISOR_TOOL_COMPLETED, SUPERVISOR_TOOL_FAILED
        """
        import asyncio
        from datetime import datetime
        from datetime import timezone

        tool_name = tool_call["name"]
        tool_args = tool_call.get("args", {})
        tool_call_id = tool_call.get("id")

        # Get worker context for tool tracking (Phase 3: still used for record_tool_start/complete)
        ctx = get_worker_context()
        tool_record = None

        # Get emitter for event emission (Phase 3: emitter handles correct event type)
        from zerg.events import get_emitter

        emitter = get_emitter()

        # Redact sensitive fields from args for event emission
        safe_args = redact_sensitive_args(tool_args)

        # Tool tracking (worker context) - kept during Phase 3 transition
        if ctx:
            tool_record = ctx.record_tool_start(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                args=safe_args,  # Redacted args (secrets masked)
            )

        # Emit STARTED event via emitter (Phase 3: emitter handles worker vs supervisor automatically)
        if emitter:
            await emitter.emit_tool_started(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_args_preview=safe_preview(str(safe_args)),
                tool_args=safe_args,  # Full args for persistence/raw view
            )

        start_time = datetime.now(timezone.utc)

        # Tools that use LangGraph's interrupt() MUST have async implementations.
        # Running them in a thread breaks the interrupt context.
        INTERRUPT_TOOLS = {"spawn_worker"}

        # Execute tool - prefer async version if available (needed for interrupt() support)
        # Tools like spawn_worker use LangGraph's interrupt() which requires running
        # in the same context as the graph, not in a separate thread.
        tool_to_call = tools_by_name.get(tool_name)

        # Fail loudly if an interrupt tool is missing its async implementation
        if tool_name in INTERRUPT_TOOLS and not getattr(tool_to_call, "coroutine", None):
            raise RuntimeError(
                f"Tool '{tool_name}' uses interrupt() and MUST have an async coroutine implementation. "
                f"Check that StructuredTool.from_function() includes coroutine=<async_fn>."
            )

        if tool_to_call and getattr(tool_to_call, "coroutine", None):
            # Tool has async implementation - call it directly
            try:
                from langgraph.errors import GraphInterrupt

                # IDEMPOTENCY: For spawn_worker, call coroutine directly to pass _tool_call_id
                # The StructuredTool.ainvoke() method uses the schema which doesn't include internal params
                if tool_name == "spawn_worker" and tool_call_id:
                    coro = tool_to_call.coroutine
                    observation = await coro(
                        task=tool_args.get("task", ""),
                        model=tool_args.get("model"),
                        _tool_call_id=tool_call_id,
                    )
                else:
                    observation = await tool_to_call.ainvoke(tool_args)
                result = ToolMessage(content=str(observation), tool_call_id=tool_call_id, name=tool_name)
            except GraphInterrupt:
                # Let interrupt bubble up to pause the graph
                raise
            except Exception as exc:
                result = ToolMessage(content=f"<tool-error> {exc}", tool_call_id=tool_call_id, name=tool_name)
                logger.exception("Error executing async tool %s", tool_name)
        else:
            # Fallback to sync version in thread
            result = await asyncio.to_thread(_call_tool_sync, tool_call)
        end_time = datetime.now(timezone.utc)
        duration_ms = int((end_time - start_time).total_seconds() * 1000)

        # Check if tool execution failed
        result_content = str(result.content) if hasattr(result, "content") else str(result)
        is_error, error_msg = check_tool_error(result_content)

        # Record metrics if collector is available
        from zerg.worker_metrics import get_metrics_collector

        metrics_collector = get_metrics_collector()
        if metrics_collector:
            metrics_collector.record_tool_call(
                tool_name=tool_name,
                start_ts=start_time,
                end_ts=end_time,
                success=not is_error,
                error=error_msg if is_error else None,
            )

            # Tier 3 telemetry: Structured logging for dev visibility (opaque to LLMs)
            # This provides real-time grep-able logs alongside metrics.jsonl for monitoring/debugging
            try:
                log_extra = {
                    "tool": tool_name,
                    "duration_ms": duration_ms,
                    "success": not is_error,
                }
                if ctx:
                    log_extra["worker_id"] = ctx.worker_id
                if is_error and error_msg:
                    log_extra["error"] = error_msg[:200]  # Truncate for log safety

                logger.info("tool_call_complete", extra=log_extra)
            except Exception:
                # Telemetry logging is best-effort - don't fail the worker
                pass

        # Tool tracking (worker context) - kept during Phase 3 transition
        if ctx and tool_record:
            if is_error:
                ctx.record_tool_complete(tool_record, success=False, error=error_msg)

                # Phase 6: Mark critical errors for fail-fast behavior
                # Determine if this is a critical error that should stop execution
                if is_critical_tool_error(result_content, error_msg, tool_name=tool_name):
                    critical_msg = _format_critical_error(tool_name, error_msg or result_content)
                    ctx.mark_critical_error(critical_msg)
                    logger.error(f"Critical tool error in worker {ctx.worker_id}: {critical_msg}")
            else:
                ctx.record_tool_complete(tool_record, success=True)

        # Emit COMPLETED/FAILED event via emitter (Phase 3: emitter handles worker vs supervisor automatically)
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

        return result

    # --- Define main entrypoint ---
    async def _agent_executor_async(
        messages: List[BaseMessage], *, previous: Optional[List[BaseMessage]] = None, enable_token_stream: bool = False
    ) -> List[BaseMessage]:
        """
        Main entrypoint for the agent. This is a simple ReAct loop:
        1. Call the model to get a response
        2. If the model calls a tool, execute it and append the result
        3. Repeat until the model generates a final response
        """
        # Initialise message history.
        #
        # ``previous`` is populated by LangGraph's *checkpointing* mechanism
        # and therefore contains the **conversation as it existed at the end
        # of the *last* agent turn*.  When the user sends a *new* message the
        # frontend creates the row in the database which is forwarded as the
        # *messages* argument while *previous* still lacks that entry.
        #
        # If we were to prefer the *previous* list we would effectively drop
        # the most recent user input – the LLM would see an outdated context
        # and produce no new assistant response.  This manifested in the UI
        # as the agent replying only to the very first user message but
        # staying silent afterwards.
        #
        # We therefore always start from the *messages* list (which is the
        # *source of truth* pulled from the database right before the
        # runnable is invoked) and *only* fall back to *previous* when the
        # caller provides an *empty* messages array (which currently never
        # happens in normal operation but keeps the function robust for
        # direct unit-tests).
        current_messages = messages or previous or []

        # --------------------------------------------------------------
        # RESUME HANDLING: Detect pending tool calls from interrupted state
        # --------------------------------------------------------------
        # If the last message is an AIMessage with tool_calls but no ToolMessage
        # response, we're resuming from an interrupt. Execute the pending tools
        # first instead of calling the LLM again (which would cause duplicate spawns).
        # --------------------------------------------------------------
        if current_messages:
            last_msg = current_messages[-1]
            if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
                # Check if there's already a ToolMessage response for these tool calls
                pending_tool_ids = {tc["id"] for tc in last_msg.tool_calls}
                responded_tool_ids = {m.tool_call_id for m in current_messages if isinstance(m, ToolMessage)}
                unresponded = pending_tool_ids - responded_tool_ids

                if unresponded:
                    # Resuming from interrupt - execute pending tools, skip initial LLM call
                    import asyncio

                    logger.info(f"Resuming with {len(unresponded)} pending tool call(s), executing tools first")
                    pending_calls = [tc for tc in last_msg.tool_calls if tc["id"] in unresponded]
                    coro_list = [_call_tool_async(tc) for tc in pending_calls]
                    tool_results = await asyncio.gather(*coro_list, return_exceptions=False)
                    current_messages = add_messages(current_messages, list(tool_results))

                    # Now call model with tool results - this becomes the "initial" call for this resume
                    llm_response = await _call_model_async(current_messages, enable_token_stream, phase="resume_synthesis")

                    # Skip the normal initial call and go straight to the tool loop
                    # (llm_response is already set, so we'll enter the while loop if needed)
                    # Jump to tool loop handling below
                    pass
                else:
                    # All tool calls have responses, proceed normally
                    llm_response = await _call_model_async(current_messages, enable_token_stream, phase="initial")
            else:
                # No pending tool calls, proceed normally
                llm_response = await _call_model_async(current_messages, enable_token_stream, phase="initial")
        else:
            # Start by calling the model with the current context
            llm_response = await _call_model_async(current_messages, enable_token_stream, phase="initial")

        # Robustness: some model/provider combinations can occasionally return an empty
        # assistant message with no tool calls. That produces "successful" but useless
        # workers (no output, no commands). Retry once with a hard constraint.
        if isinstance(llm_response, AIMessage) and not llm_response.tool_calls:
            content = llm_response.content
            content_text = ""
            if isinstance(content, list):
                # Multimodal blocks: collect any text.
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        content_text += str(part.get("text") or "")
                    elif isinstance(part, str):
                        content_text += part
            else:
                content_text = str(content or "")

            if not content_text.strip():
                from langchain_core.messages import SystemMessage

                logger.warning("Agent produced empty response with no tool calls; retrying once")
                current_messages = add_messages(
                    current_messages,
                    [
                        SystemMessage(
                            content=(
                                "Your previous response was empty. You MUST either:\n"
                                "1) Call the appropriate tool(s), OR\n"
                                "2) Provide a final answer.\n\n"
                                "Do not return an empty message."
                            )
                        )
                    ],
                )
                # Force tool invocation on retry when tools exist. Some provider/model
                # combinations can return empty content without tool calls; "required"
                # makes the failure mode observable (tool error) instead of silent.
                llm_response = await _call_model_async(
                    current_messages,
                    enable_token_stream,
                    phase="empty_retry",
                    tool_choice="required" if tools else None,
                )

                # If the provider still returns an empty assistant message, surface a
                # concrete failure message so workers don't "succeed" with no output.
                if isinstance(llm_response, AIMessage) and not llm_response.tool_calls:
                    retry_content = llm_response.content
                    retry_text = ""
                    if isinstance(retry_content, list):
                        for part in retry_content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                retry_text += str(part.get("text") or "")
                            elif isinstance(part, str):
                                retry_text += part
                    else:
                        retry_text = str(retry_content or "")

                    if not retry_text.strip():
                        logger.error("Agent produced empty response even after tool_choice=required retry")
                        llm_response = AIMessage(
                            content=(
                                "Error: LLM returned an empty response twice (no tool calls, no text). "
                                "This is a provider/model issue; retry the worker or switch the worker model."
                            )
                        )

        # Until the model stops calling tools, continue the loop
        import asyncio

        while isinstance(llm_response, AIMessage) and llm_response.tool_calls:
            # --------------------------------------------------------------
            # True *parallel* tool execution
            # --------------------------------------------------------------
            # Convert every tool call into an **awaitable** coroutine and run
            # them concurrently via ``asyncio.gather``.  Errors inside an
            # individual tool no longer block the whole batch – the
            # *observation* string will contain the exception text which the
            # LLM can reason about in the next turn.
            # --------------------------------------------------------------

            # CRITICAL: Add AIMessage to history BEFORE executing tools.
            # This ensures the checkpoint captures the tool call if interrupt() is raised.
            # Without this, resume would see only [system, human] and re-call the LLM,
            # causing duplicate worker spawns.
            current_messages = add_messages(current_messages, [llm_response])

            # Store the AIMessage in context var so agent_runner can persist it if interrupt occurs.
            # LangGraph's functional API doesn't include messages in interrupt result, so we need
            # this mechanism to ensure the AIMessage with tool_calls gets saved to thread DB.
            set_pending_ai_message(llm_response)

            coro_list = [_call_tool_async(tc) for tc in llm_response.tool_calls]
            tool_results = await asyncio.gather(*coro_list, return_exceptions=False)

            # Tools completed without interrupt - clear the pending message
            clear_pending_ai_message()

            # Add tool results to history
            current_messages = add_messages(current_messages, list(tool_results))

            # Phase 6: Check for critical errors and fail fast
            # If a critical tool error occurred, stop execution immediately
            ctx = get_worker_context()
            if ctx and ctx.has_critical_error:
                logger.warning(f"Worker {ctx.worker_id} stopping due to critical error: {ctx.critical_error_message}")
                # Create final assistant message explaining the failure
                from langchain_core.messages import AIMessage as FinalAIMessage

                error_response = FinalAIMessage(
                    content=(
                        f"I encountered a critical error that prevents me from completing this task:\n\n" f"{ctx.critical_error_message}"
                    )
                )
                final_messages = add_messages(current_messages, [error_response])
                return final_messages

            # Call model again with updated messages
            llm_response = await _call_model_async(current_messages, enable_token_stream, phase="tool_iteration")

        # Add the final response to history
        final_messages = add_messages(current_messages, [llm_response])

        # Return the full conversation history
        return final_messages

    # ------------------------------------------------------------------
    # Synchronous wrapper for libraries/tests that call ``.invoke``
    # ------------------------------------------------------------------

    def _agent_executor_sync(
        messages: List[BaseMessage],
        *,
        previous: Optional[List[BaseMessage]] = None,
        enable_token_stream: bool = False,
    ):
        """Blocking wrapper that delegates to the async implementation using shared runner."""

        from zerg.utils.async_runner import run_in_shared_loop

        return run_in_shared_loop(_agent_executor_async(messages, previous=previous, enable_token_stream=enable_token_stream))

    # ------------------------------------------------------------------
    # Expose BOTH sync & async entrypoints to LangGraph
    # ------------------------------------------------------------------
    # Read enable_token_stream at invocation time, not at runnable creation time
    # This allows the LLM_TOKEN_STREAM environment variable to be changed dynamically

    @entrypoint(checkpointer=checkpointer)
    def agent_executor(messages: List[BaseMessage], *, previous: Optional[List[BaseMessage]] = None):
        # Token streaming is only useful for user-facing supervisor/Jarvis flows.
        # Workers run in the background and don't have the WS/SSE context needed
        # to deliver tokens; disabling streaming avoids extra overhead and weird
        # edge cases where providers return empty final content.
        enable_token_stream = get_settings().llm_token_stream and get_worker_context() is None
        return _agent_executor_sync(messages, previous=previous, enable_token_stream=enable_token_stream)

    # Attach the *async* implementation manually – LangGraph picks this up so
    # callers can use ``.ainvoke`` while tests and legacy code continue to use
    # the blocking ``.invoke`` API.

    async def _agent_executor_async_wrapper(messages: List[BaseMessage], *, previous: Optional[List[BaseMessage]] = None):
        enable_token_stream = get_settings().llm_token_stream and get_worker_context() is None
        return await _agent_executor_async(messages, previous=previous, enable_token_stream=enable_token_stream)

    agent_executor.afunc = _agent_executor_async_wrapper  # type: ignore[attr-defined]

    return agent_executor


# ---------------------------------------------------------------------------
# Helper – preserve for unit-testing & potential reuse
# ---------------------------------------------------------------------------


def get_tool_messages(ai_msg: AIMessage):  # noqa: D401 – util function
    """Return a list of ToolMessage instances for each tool call in *ai_msg*.

    This helper is mainly used in unit-tests but can also aid debugging in a
    REPL. It was removed during an earlier refactor and has been reinstated to
    keep backwards-compatibility with the test-suite.
    """

    if not getattr(ai_msg, "tool_calls", None):
        return []

    # Import builtin tools to ensure they're registered

    # Get the tool resolver
    resolver = get_tool_resolver()

    tool_messages: List[ToolMessage] = []
    for tc in ai_msg.tool_calls:
        name = tc.get("name")
        content = "<no-op>"
        try:
            # Resolve the tool – tests may monkeypatch the **module-level**
            # reference (``zerg.agents_def.zerg_react_agent.get_current_time``)
            # so we first look it up dynamically on the module and fall back
            # to the registry entry.

            import sys

            module_tool = getattr(sys.modules[__name__], name, None)
            tool = module_tool or resolver.get_tool(name)

            if tool is not None:
                content = tool.invoke(tc.get("args", {}))
            else:
                available_tools = resolver.get_tool_names()
                content = f"<tool-error> Tool '{name}' not found. Available: {available_tools}"
        except Exception as exc:  # noqa: BLE001
            content = f"<tool-error> {exc}"

        tool_messages.append(ToolMessage(content=str(content), tool_call_id=tc.get("id"), name=name))

    return tool_messages


# ---------------------------------------------------------------------------
# Backward compatibility - expose get_current_time at module level
# ---------------------------------------------------------------------------
# Import builtin tools to ensure registration
import zerg.tools.builtin  # noqa: F401, E402

# Get the tool from resolver and expose it at module level for tests
_resolver = get_tool_resolver()
get_current_time = _resolver.get_tool("get_current_time")
