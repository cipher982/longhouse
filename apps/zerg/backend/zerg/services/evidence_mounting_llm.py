"""Evidence Mounting LLM – wrapper that expands evidence markers before each LLM call.

This module implements Phase 2 of the Mount → Reason → Prune system.
It wraps the base LLM to detect and expand evidence markers in ToolMessages
before each API call within the ReAct loop.

Key Insights:
- The ReAct loop makes MULTIPLE LLM calls per agent invocation
- Evidence must be mounted before EACH call, not just once at run start
- Expansion happens OUTSIDE LangGraph's state machine (no persistence)
- Only the compact payload (with marker) is persisted to thread_messages

Architecture:
- Wraps any LangChain-compatible LLM (ChatOpenAI, etc.)
- Detects [EVIDENCE:...] markers in ToolMessage content
- Expands markers using EvidenceCompiler (reads from artifact store)
- Returns modified messages (copies, never mutates originals)
- Transparent to the rest of the system (pass-through if no markers/context)

References:
- docs/specs/MOUNT_REASON_PRUNE_IMPLEMENTATION.md
- zerg/services/evidence_compiler.py (Phase 1)
"""

from __future__ import annotations

import logging
import re
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.messages import ToolMessage

from zerg.services.evidence_compiler import EvidenceCompiler

logger = logging.getLogger(__name__)

# Regex to detect evidence markers in ToolMessage content
# Format: [EVIDENCE:run_id=48,job_id=123,worker_id=abc-123]
EVIDENCE_MARKER_PATTERN = re.compile(
    r"\[EVIDENCE:run_id=(\d+),job_id=(\d+),worker_id=([^\]]+)\]",
    re.IGNORECASE,
)


class EvidenceMountingLLM:
    """Wraps base LLM to mount evidence before each API call.

    This wrapper intercepts LLM calls and expands evidence markers in ToolMessages.
    It's designed to work with LangGraph's ReAct loop without polluting the
    persistent message state.

    Usage:
        # Create wrapper (typically in zerg_react_agent.py)
        base_llm = ChatOpenAI(model="gpt-4")
        wrapped_llm = EvidenceMountingLLM(
            base_llm=base_llm,
            run_id=supervisor_run.id,
            owner_id=supervisor_run.owner_id,
        )

        # Use normally - evidence mounting is transparent
        response = await wrapped_llm.ainvoke(messages)

    The wrapper only mounts evidence if:
    1. run_id and owner_id are provided (supervisor context)
    2. Messages contain [EVIDENCE:...] markers
    3. EvidenceCompiler can access the worker artifacts

    Otherwise, it passes through to the base LLM unchanged.
    """

    def __init__(
        self,
        base_llm: Any,
        run_id: int | None = None,
        owner_id: int | None = None,
        db: Any = None,
    ):
        """Initialize the evidence mounting LLM wrapper.

        Parameters
        ----------
        base_llm
            The base LLM to wrap (e.g., ChatOpenAI)
        run_id
            Supervisor run ID for evidence correlation (None = no mounting)
        owner_id
            User ID for security scoping (None = no mounting)
        db
            Database session for evidence compiler (None = no mounting)
        """
        self.base_llm = base_llm
        self.run_id = run_id
        self.owner_id = owner_id
        self.db = db
        self.compiler = EvidenceCompiler(db=db)

    async def ainvoke(self, messages: list[BaseMessage], **kwargs) -> Any:
        """Invoke the LLM with evidence mounting.

        This method:
        1. Checks if we have supervisor context (run_id + owner_id + db)
        2. Scans messages for evidence markers
        3. Expands markers using EvidenceCompiler
        4. Calls base LLM with augmented messages
        5. Returns result (expansion never persisted)

        Parameters
        ----------
        messages
            List of LangChain messages (AIMessage, ToolMessage, etc.)
        **kwargs
            Additional arguments passed to base LLM

        Returns
        -------
        Any
            Result from base LLM (typically AIMessage)
        """
        # Only mount if we have supervisor context
        if self.run_id is not None and self.owner_id is not None and self.db is not None:
            try:
                messages = self._mount_evidence(messages)
            except Exception as e:
                # Evidence mounting is best-effort - don't fail the LLM call
                logger.warning(f"Evidence mounting failed for run_id={self.run_id}: {e}", exc_info=True)

        # Delegate to base LLM
        return await self.base_llm.ainvoke(messages, **kwargs)

    def _mount_evidence(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        """Detect evidence markers and expand them with compiled evidence.

        This method:
        1. Scans all ToolMessages for [EVIDENCE:...] markers
        2. Extracts marker parameters (run_id, job_id, worker_id)
        3. Calls EvidenceCompiler once to get all evidence for this run
        4. Replaces/appends evidence to matching ToolMessages
        5. Returns copied messages (never mutates originals)

        Parameters
        ----------
        messages
            Original message list

        Returns
        -------
        list[BaseMessage]
            Augmented message list (copies with evidence expanded)
        """
        # Quick scan: do we have any evidence markers?
        has_markers = any(isinstance(msg, ToolMessage) and EVIDENCE_MARKER_PATTERN.search(str(msg.content)) for msg in messages)

        if not has_markers:
            return messages  # No markers, pass through

        # Compile evidence once for all workers in this run
        try:
            evidence_map = self.compiler.compile(
                run_id=self.run_id,
                owner_id=self.owner_id,
                db=self.db,
            )
        except Exception as e:
            logger.error(f"EvidenceCompiler.compile() failed for run_id={self.run_id}: {e}", exc_info=True)
            return messages  # Fail gracefully, return original messages

        if not evidence_map:
            logger.debug(f"No evidence available for run_id={self.run_id}")
            return messages

        # Process messages and expand markers
        augmented_messages = []
        for msg in messages:
            if isinstance(msg, ToolMessage):
                augmented_msg = self._expand_tool_message(msg, evidence_map)
                augmented_messages.append(augmented_msg)
            else:
                # Non-ToolMessage: pass through as-is
                augmented_messages.append(msg)

        logger.info(f"Mounted evidence for run_id={self.run_id}: {len(evidence_map)} workers")
        return augmented_messages

    def _expand_tool_message(self, msg: ToolMessage, evidence_map: dict[int, str]) -> ToolMessage:
        """Expand evidence markers in a single ToolMessage.

        Parameters
        ----------
        msg
            Original ToolMessage (may contain evidence marker)
        evidence_map
            Mapping of job_id -> expanded evidence string

        Returns
        -------
        ToolMessage
            New ToolMessage with evidence expanded (or original if no marker)
        """
        content = str(msg.content)
        match = EVIDENCE_MARKER_PATTERN.search(content)

        if not match:
            return msg  # No marker, return as-is

        # Extract marker parameters
        marker_run_id = int(match.group(1))
        marker_job_id = int(match.group(2))
        marker_worker_id = match.group(3)

        # Validate marker matches our context
        if marker_run_id != self.run_id:
            logger.warning(f"Evidence marker run_id mismatch: marker={marker_run_id}, context={self.run_id}. Skipping expansion.")
            return msg

        # Get evidence for this worker
        evidence = evidence_map.get(marker_job_id)
        if not evidence:
            logger.warning(f"No evidence found for job_id={marker_job_id} (worker_id={marker_worker_id})")
            # Return original message with a note
            expanded_content = content.replace(match.group(0), f"{match.group(0)}\n\n[Evidence unavailable for this worker]")
            return ToolMessage(
                content=expanded_content,
                tool_call_id=msg.tool_call_id,
                name=msg.name,
            )

        # Replace marker with expanded evidence
        expanded_content = content.replace(match.group(0), f"{match.group(0)}\n\n{evidence}")

        # Create new ToolMessage (copy, don't mutate)
        return ToolMessage(
            content=expanded_content,
            tool_call_id=msg.tool_call_id,
            name=msg.name,
        )

    # Pass through other LLM methods to base_llm
    def bind_tools(self, tools, **kwargs):
        """Bind tools to the base LLM."""
        # Return a new wrapper with tools bound to base LLM
        bound_llm = self.base_llm.bind_tools(tools, **kwargs)
        return EvidenceMountingLLM(
            base_llm=bound_llm,
            run_id=self.run_id,
            owner_id=self.owner_id,
            db=self.db,
        )

    def __getattr__(self, name):
        """Delegate all other attributes/methods to base LLM."""
        return getattr(self.base_llm, name)


__all__ = ["EvidenceMountingLLM", "EVIDENCE_MARKER_PATTERN"]
