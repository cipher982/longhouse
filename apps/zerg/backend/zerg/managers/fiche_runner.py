"""Runner -- asynchronous one-turn execution helper.

Bridges Fiche ORM row, ThreadService for DB persistence, and the
ReAct execution loop (LangGraph-free).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from typing import List
from typing import Sequence

from sqlalchemy.orm import Session

from zerg.callbacks.token_stream import set_current_thread_id
from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.crud import crud
from zerg.models.models import Fiche as FicheModel
from zerg.models.models import Thread as ThreadModel
from zerg.models.models import ThreadMessage as ThreadMessageModel
from zerg.services.thread_service import ThreadService

logger = logging.getLogger(__name__)


class FicheInterrupted(Exception):
    """Raised when agent execution is interrupted (waiting for external input)."""

    def __init__(self, interrupt_value: dict):
        self.interrupt_value = interrupt_value
        super().__init__(f"Fiche interrupted: {interrupt_value}")


@dataclass(frozen=True)
class RuntimeView:
    """Read-only runtime view of a Fiche row.

    Avoids mutating the SQLAlchemy-managed Fiche ORM object.
    Per-request overrides (model, reasoning_effort) must not be persisted.
    """

    id: int
    owner_id: int
    updated_at: Any
    model: str
    config: dict
    allowed_tools: Any


class Runner:  # noqa: D401
    """Run one agent turn (async)."""

    def __init__(
        self,
        agent_row: FicheModel,
        *,
        thread_service: ThreadService | None = None,
        model_override: str | None = None,
        reasoning_effort: str | None = None,
    ):
        runtime_cfg = dict(getattr(agent_row, "config", {}) or {})
        if reasoning_effort is not None:
            runtime_cfg["reasoning_effort"] = reasoning_effort

        self.agent = RuntimeView(
            id=agent_row.id,
            owner_id=agent_row.owner_id,
            updated_at=getattr(agent_row, "updated_at", None),
            model=model_override or agent_row.model,
            config=runtime_cfg,
            allowed_tools=getattr(agent_row, "allowed_tools", None),
        )
        self.thread_service = thread_service or ThreadService

        # Aggregated usage for the last run
        self.usage_prompt_tokens: int | None = None
        self.usage_completion_tokens: int | None = None
        self.usage_total_tokens: int | None = None
        self.usage_reasoning_tokens: int | None = None

        from zerg.config import get_settings

        self.enable_token_stream = get_settings().llm_token_stream

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _load_agent_row(self, db: Session) -> FicheModel:
        agent_row = crud.get_fiche(db, self.agent.id)
        if not agent_row or not agent_row.system_instructions:
            raise RuntimeError(f"Fiche {self.agent.id} has no system_instructions")
        return agent_row

    def _resolve_tools(self, db: Session, agent_row: FicheModel, skill_integration: Any) -> list:
        from zerg.tools import get_registry

        resolver = get_registry()
        tools = resolver.filter_by_allowlist(agent_row.allowed_tools)
        if skill_integration:
            tool_map = {tool.name: tool for tool in tools}
            try:
                skill_tools = skill_integration.get_tools(tool_map)
                if skill_tools:
                    tools = tools + skill_tools
            except Exception as e:
                logger.warning("[Runner] Failed to build skill tools: %s", e, exc_info=True)
        return tools

    async def _run_loop(
        self,
        db: Session,
        thread: ThreadModel,
        messages: list,
        agent_row: FicheModel,
        skill_integration: Any,
        *,
        run_id: int | None = None,
        trace_id: str | None = None,
    ) -> Any:
        """Set up contexts, run oikos loop, capture usage. Returns OikosResult."""
        from zerg.callbacks.token_stream import reset_current_thread_id
        from zerg.connectors.context import reset_credential_resolver
        from zerg.services.oikos_react_engine import reset_llm_usage
        from zerg.services.oikos_react_engine import run_oikos_loop

        credential_resolver = CredentialResolver(
            fiche_id=self.agent.id,
            db=db,
            owner_id=self.agent.owner_id,
        )
        _cred_ctx_token = set_credential_resolver(credential_resolver)
        _ctx_token = set_current_thread_id(thread.id)

        try:
            tools = self._resolve_tools(db, agent_row, skill_integration)
            reset_llm_usage()

            # Resolve run_id/trace_id from oikos context if not provided
            if run_id is None or trace_id is None:
                from zerg.context import get_commis_context
                from zerg.services.oikos_context import get_oikos_context

                sup_ctx = get_oikos_context()
                commis_ctx = get_commis_context()
                if run_id is None:
                    run_id = sup_ctx.run_id if sup_ctx else None
                if trace_id is None:
                    trace_id = sup_ctx.trace_id if sup_ctx else (commis_ctx.trace_id if commis_ctx else None)

            result = await run_oikos_loop(
                messages=messages,
                fiche_row=self.agent,
                tools=tools,
                run_id=run_id,
                owner_id=self.agent.owner_id,
                trace_id=trace_id,
                enable_token_stream=self.enable_token_stream,
            )

            # Capture usage
            self.usage_prompt_tokens = result.usage.get("prompt_tokens")
            self.usage_completion_tokens = result.usage.get("completion_tokens")
            self.usage_total_tokens = result.usage.get("total_tokens")
            self.usage_reasoning_tokens = result.usage.get("reasoning_tokens")

            return result
        finally:
            reset_current_thread_id(_ctx_token)
            reset_credential_resolver(_cred_ctx_token)

    def _handle_new_messages(
        self,
        db: Session,
        thread: ThreadModel,
        result_messages: list,
        messages_with_context: int,
    ) -> list[ThreadMessageModel]:
        """Extract, persist, and return new messages from loop result."""
        if len(result_messages) <= messages_with_context:
            logger.warning("No new messages generated for thread %s", thread.id)
            return []

        new_messages = result_messages[messages_with_context:]
        new_messages = [m for m in new_messages if not (hasattr(m, "type") and m.type == "system")]

        created_rows = self.thread_service.save_new_messages(
            db,
            thread_id=thread.id,
            messages=new_messages,
            processed=True,
        )
        logger.info(f"[Runner] Saved {len(created_rows)} message rows", extra={"tag": "AGENT"})

        self._persist_usage(db, created_rows)
        self.thread_service.touch_thread_timestamp(db, thread.id)
        return created_rows

    def _persist_usage(self, db: Session, created_rows: list) -> None:
        usage = {
            "prompt_tokens": self.usage_prompt_tokens,
            "completion_tokens": self.usage_completion_tokens,
            "total_tokens": self.usage_total_tokens,
            "reasoning_tokens": self.usage_reasoning_tokens,
        }
        if not any(v is not None for v in usage.values()):
            return
        last_assistant = next(
            (row for row in reversed(created_rows) if row.role == "assistant"),
            None,
        )
        if last_assistant is not None:
            meta = dict(last_assistant.message_metadata or {})
            meta["usage"] = usage
            last_assistant.message_metadata = meta
            db.commit()

    def _handle_interrupt(
        self,
        db: Session,
        thread: ThreadModel,
        result: Any,
        messages_with_context: int,
    ) -> None:
        """Persist partial messages and raise FicheInterrupted."""
        if len(result.messages) > messages_with_context:
            new_messages = result.messages[messages_with_context:]
            new_messages = [m for m in new_messages if not (hasattr(m, "type") and m.type == "system")]
            if new_messages:
                created_rows = self.thread_service.save_new_messages(
                    db,
                    thread_id=thread.id,
                    messages=new_messages,
                    processed=True,
                )
                self._persist_usage(db, created_rows)
        raise FicheInterrupted(result.interrupt_value or {})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_thread(self, db: Session, thread: ThreadModel) -> Sequence[ThreadMessageModel]:
        """Process unprocessed messages and return created assistant message rows."""
        from zerg.managers.prompt_context import build_prompt
        from zerg.managers.prompt_context import context_to_messages

        agent_row = self._load_agent_row(db)

        unprocessed_rows = crud.get_unprocessed_messages(db, thread.id)
        if not unprocessed_rows:
            return []

        prompt_context = build_prompt(
            db,
            self.agent,
            agent_row,
            thread_id=thread.id,
            unprocessed_rows=unprocessed_rows,
            allowed_tools=getattr(agent_row, "allowed_tools", None),
            thread_service=self.thread_service,
        )
        messages = context_to_messages(prompt_context)
        messages_with_context = prompt_context.message_count_with_context

        result = await self._run_loop(db, thread, messages, agent_row, prompt_context.skill_integration)

        if result.interrupted:
            self.thread_service.mark_messages_processed(db, (row.id for row in unprocessed_rows))
            self.thread_service.touch_thread_timestamp(db, thread.id)
            self._handle_interrupt(db, thread, result, messages_with_context)

        created_rows = self._handle_new_messages(db, thread, result.messages, messages_with_context)
        self.thread_service.mark_messages_processed(db, (row.id for row in unprocessed_rows))

        if unprocessed_rows and not created_rows:
            raise RuntimeError("Fiche produced no messages despite pending user input.")

        return created_rows

    # ------------------------------------------------------------------
    # Continuation API (commis resume)
    # ------------------------------------------------------------------

    async def run_continuation(
        self,
        db: Session,
        thread: ThreadModel,
        tool_call_id: str,
        tool_result: str,
        *,
        run_id: int | None = None,
        trace_id: str | None = None,
    ) -> Sequence[ThreadMessageModel]:
        """Continue oikos execution after a single commis completion."""
        from zerg.managers.prompt_context import build_prompt
        from zerg.managers.prompt_context import context_to_messages
        from zerg.types.messages import ToolMessage

        agent_row = self._load_agent_row(db)
        db_messages = self.thread_service.get_thread_messages_as_langchain(db, thread.id)

        # Idempotency: skip if ToolMessage already exists for this tool_call_id
        existing = any(isinstance(m, ToolMessage) and m.tool_call_id == tool_call_id for m in db_messages)
        tool_msg: ToolMessage | None = None

        if not existing:
            tool_msg = ToolMessage(
                content=f"Commis completed:\n\n{tool_result}",
                tool_call_id=tool_call_id,
                name="spawn_commis",
            )
            parent_id = self._find_parent_assistant(db, thread.id, tool_call_id)
            self.thread_service.save_new_messages(
                db,
                thread_id=thread.id,
                messages=[tool_msg],
                processed=True,
                parent_id=parent_id,
            )

        prompt_context = build_prompt(
            db,
            self.agent,
            agent_row,
            conversation_msgs=list(db_messages),
            tool_messages=[tool_msg] if tool_msg is not None else None,
            allowed_tools=getattr(agent_row, "allowed_tools", None),
        )
        messages = context_to_messages(prompt_context)
        messages_with_context = prompt_context.message_count_with_context

        result = await self._run_loop(
            db,
            thread,
            messages,
            agent_row,
            prompt_context.skill_integration,
            run_id=run_id,
            trace_id=trace_id,
        )

        if result.interrupted:
            self._handle_interrupt(db, thread, result, messages_with_context)

        return self._handle_new_messages(db, thread, result.messages, messages_with_context)

    async def run_batch_continuation(
        self,
        db: Session,
        thread: ThreadModel,
        commis_results: list[dict],
        *,
        run_id: int | None = None,
        trace_id: str | None = None,
    ) -> Sequence[ThreadMessageModel]:
        """Continue oikos execution after ALL commiss complete (barrier pattern)."""
        from zerg.managers.prompt_context import build_prompt
        from zerg.managers.prompt_context import context_to_messages
        from zerg.types.messages import ToolMessage

        agent_row = self._load_agent_row(db)
        db_messages = self.thread_service.get_thread_messages_as_langchain(db, thread.id)

        # Find parent assistant message for UI grouping
        from zerg.models.thread import ThreadMessage as TMModel

        parent_msg = (
            db.query(TMModel)
            .filter(TMModel.thread_id == thread.id, TMModel.role == "assistant", TMModel.tool_calls.isnot(None))
            .order_by(TMModel.sent_at.desc())
            .first()
        )
        parent_id = parent_msg.id if parent_msg else None

        # Create ToolMessages for all commis results (with idempotency)
        tool_msgs_to_inject: List[ToolMessage] = []
        for wr in commis_results:
            tcid = wr.get("tool_call_id")
            result_text = wr.get("result") or ""
            error = wr.get("error")
            status = wr.get("status", "completed")

            if any(isinstance(m, ToolMessage) and m.tool_call_id == tcid for m in db_messages):
                continue

            content = (
                f"Commis failed:\n\nError: {error}\n\nPartial result: {result_text}"
                if error or status == "failed"
                else f"Commis completed:\n\n{result_text}"
            )
            tool_msg = ToolMessage(content=content, tool_call_id=tcid, name="spawn_commis")
            self.thread_service.save_new_messages(
                db,
                thread_id=thread.id,
                messages=[tool_msg],
                processed=True,
                parent_id=parent_id,
            )
            tool_msgs_to_inject.append(tool_msg)

        prompt_context = build_prompt(
            db,
            self.agent,
            agent_row,
            conversation_msgs=list(db_messages),
            tool_messages=tool_msgs_to_inject if tool_msgs_to_inject else None,
            allowed_tools=getattr(agent_row, "allowed_tools", None),
        )
        messages = context_to_messages(prompt_context)
        messages_with_context = prompt_context.message_count_with_context

        result = await self._run_loop(
            db,
            thread,
            messages,
            agent_row,
            prompt_context.skill_integration,
            run_id=run_id,
            trace_id=trace_id,
        )

        if result.interrupted:
            self._handle_interrupt(db, thread, result, messages_with_context)

        return self._handle_new_messages(db, thread, result.messages, messages_with_context)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _find_parent_assistant(self, db: Session, thread_id: int, tool_call_id: str) -> int | None:
        """Find the assistant message that issued a specific tool call."""
        from zerg.models.thread import ThreadMessage as TMModel

        rows = (
            db.query(TMModel)
            .filter(TMModel.thread_id == thread_id, TMModel.role == "assistant", TMModel.tool_calls.isnot(None))
            .order_by(TMModel.sent_at.desc())
            .all()
        )
        for msg in rows:
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    if tc.get("id") == tool_call_id:
                        return msg.id
        return None


FicheRunner = Runner
