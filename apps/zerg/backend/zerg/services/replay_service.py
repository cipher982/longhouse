"""
Replay service for deterministic demo recordings.

ONLY available when REPLAY_MODE_ENABLED=true (dev only).
This service allows video recording scripts to get consistent, predictable
responses without hitting the real LLM.

Usage:
    1. Set REPLAY_MODE_ENABLED=true in environment
    2. Create scenario YAML files in scripts/video-scenarios/
    3. Pass ?replay=scenario-name in the chat URL
    4. Frontend passes replay scenario to backend via request body
"""

import asyncio
import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Environment gate - MUST be explicitly enabled
REPLAY_MODE_ENABLED = os.getenv("REPLAY_MODE_ENABLED", "").lower() == "true"


def is_replay_enabled() -> bool:
    """Check if replay mode is enabled."""
    return REPLAY_MODE_ENABLED


class ReplayService:
    """Service for replaying golden conversations from scenario files.

    Loads scenario YAML files and provides conversation data for
    deterministic video recording.
    """

    def __init__(self, scenario_name: str):
        """Initialize replay service with a scenario.

        Args:
            scenario_name: Name of the scenario file (without .yaml extension)

        Raises:
            RuntimeError: If replay mode is not enabled
            FileNotFoundError: If scenario file doesn't exist
        """
        if not REPLAY_MODE_ENABLED:
            raise RuntimeError("Replay mode not enabled. Set REPLAY_MODE_ENABLED=true to use.")

        self.scenario_name = scenario_name
        self.scenario = self._load_scenario(scenario_name)
        self.conversations = self.scenario.get("golden_conversations", {})

        logger.info(f"ReplayService initialized with scenario: {scenario_name}")

    def _load_scenario(self, name: str) -> dict:
        """Load scenario YAML file.

        Looks for scenarios in scripts/video-scenarios/ relative to repo root.
        """
        # Navigate from apps/zerg/backend/zerg/services/ to repo root
        # parents: [0]=services, [1]=zerg, [2]=backend, [3]=zerg, [4]=apps, [5]=repo_root
        repo_root = Path(__file__).parents[5]
        path = repo_root / "scripts" / "video-scenarios" / f"{name}.yaml"

        if not path.exists():
            raise FileNotFoundError(f"Scenario not found: {path}")

        with open(path) as f:
            return yaml.safe_load(f)

    def get_conversation(self, conversation_id: str) -> dict | None:
        """Get golden conversation data by ID.

        Args:
            conversation_id: ID of the conversation from scenario

        Returns:
            Conversation dict with messages, worker_result, final_message,
            or None if not found
        """
        return self.conversations.get(conversation_id)

    def match_conversation(self, user_message: str) -> str | None:
        """Find a golden conversation that matches the user message.

        Args:
            user_message: The user's message text

        Returns:
            Conversation ID if found, None otherwise
        """
        user_message_lower = user_message.lower().strip()

        for conv_id, conv_data in self.conversations.items():
            expected = conv_data.get("user_message", "").lower().strip()
            # Exact match or high similarity
            if expected and (user_message_lower == expected or expected in user_message_lower):
                logger.info(f"Matched user message to conversation: {conv_id}")
                return conv_id

        logger.debug(f"No matching conversation for: {user_message[:50]}...")
        return None

    async def emit_conversation_events(
        self,
        conversation_id: str,
        run_id: int,
        thread_id: int,
        owner_id: int,
        message_id: str,
        trace_id: str,
    ) -> None:
        """Emit SSE events for a golden conversation.

        This simulates the real supervisor flow by emitting events
        with the correct timing and data.

        Args:
            conversation_id: ID of the golden conversation
            run_id: The agent run ID
            thread_id: The thread ID
            owner_id: User ID
            message_id: Client message ID
            trace_id: Trace ID for debugging
        """
        from zerg.events import EventType
        from zerg.events.event_bus import event_bus

        conv = self.conversations.get(conversation_id)
        if not conv:
            logger.error(f"Conversation not found: {conversation_id}")
            return

        # Emit supervisor_started
        await event_bus.publish(
            EventType.CONCIERGE_STARTED,
            {
                "event_type": "concierge_started",
                "run_id": run_id,
                "thread_id": thread_id,
                "owner_id": owner_id,
                "message_id": message_id,
                "trace_id": trace_id,
                "task": conv.get("user_message", ""),
            },
        )

        # Emit thinking
        await event_bus.publish(
            EventType.CONCIERGE_THINKING,
            {
                "event_type": "concierge_thinking",
                "run_id": run_id,
                "owner_id": owner_id,
                "message": "Analyzing your request...",
                "trace_id": trace_id,
            },
        )

        # Process messages sequence
        for msg in conv.get("messages", []):
            delay_ms = msg.get("delay_ms", 500)
            await asyncio.sleep(delay_ms / 1000)

            if msg["role"] == "assistant":
                # Stream tokens for realistic typing effect
                content = msg.get("content", "")
                await self._stream_tokens(
                    content=content,
                    run_id=run_id,
                    owner_id=owner_id,
                    message_id=message_id,
                    trace_id=trace_id,
                )

            elif msg["role"] == "tool_use":
                # Emit tool events
                tool_name = msg.get("name", "")
                tool_input = msg.get("input", {})

                if tool_name == "spawn_commis":
                    # Emit worker spawned event
                    await event_bus.publish(
                        EventType.COMMIS_SPAWNED,
                        {
                            "event_type": "commis_spawned",
                            "run_id": run_id,
                            "owner_id": owner_id,
                            "job_id": 999,  # Fake job ID
                            "tool_call_id": f"call_{run_id}_spawn",
                            "task": tool_input.get("task", ""),
                            "trace_id": trace_id,
                        },
                    )

        # Simulate worker completion if present
        if worker_result := conv.get("worker_result"):
            delay_ms = worker_result.get("delay_ms", 2000)
            await asyncio.sleep(delay_ms / 1000)

            # Emit worker complete
            await event_bus.publish(
                EventType.COMMIS_COMPLETE,
                {
                    "event_type": "commis_complete",
                    "run_id": run_id,
                    "owner_id": owner_id,
                    "job_id": 999,
                    "tool_call_id": f"call_{run_id}_spawn",
                    "status": "success",
                    "result": worker_result.get("result", ""),
                    "trace_id": trace_id,
                },
            )

        # Emit final message
        if final_msg := conv.get("final_message"):
            delay_ms = final_msg.get("delay_ms", 500)
            await asyncio.sleep(delay_ms / 1000)

            content = final_msg.get("content", "")
            await self._stream_tokens(
                content=content,
                run_id=run_id,
                owner_id=owner_id,
                message_id=message_id,
                trace_id=trace_id,
            )

        # Emit completion
        await event_bus.publish(
            EventType.CONCIERGE_COMPLETE,
            {
                "event_type": "concierge_complete",
                "run_id": run_id,
                "thread_id": thread_id,
                "owner_id": owner_id,
                "message_id": message_id,
                "status": "success",
                "result": conv.get("final_message", {}).get("content", ""),
                "trace_id": trace_id,
            },
        )

        logger.info(f"Replay completed for conversation: {conversation_id}")

    async def _stream_tokens(
        self,
        content: str,
        run_id: int,
        owner_id: int,
        message_id: str,
        trace_id: str,
        chunk_size: int = 5,
        delay_ms: int = 30,
    ) -> None:
        """Stream content as token events for realistic typing effect.

        Args:
            content: Text to stream
            run_id: Run ID
            owner_id: User ID
            message_id: Message ID
            trace_id: Trace ID
            chunk_size: Number of characters per chunk
            delay_ms: Delay between chunks in milliseconds
        """
        from zerg.events import EventType
        from zerg.events.event_bus import event_bus

        # Split into chunks
        chunks = [content[i : i + chunk_size] for i in range(0, len(content), chunk_size)]

        for chunk in chunks:
            await event_bus.publish(
                EventType.CONCIERGE_TOKEN,
                {
                    "event_type": "concierge_token",
                    "run_id": run_id,
                    "owner_id": owner_id,
                    "message_id": message_id,
                    "token": chunk,
                    "trace_id": trace_id,
                },
            )
            await asyncio.sleep(delay_ms / 1000)


async def run_replay_conversation(
    scenario_name: str,
    user_message: str,
    run_id: int,
    thread_id: int,
    owner_id: int,
    message_id: str,
    trace_id: str,
) -> bool:
    """Run a replay conversation if matching golden data exists.

    This is the main entry point for replay mode. It:
    1. Loads the scenario
    2. Matches the user message to a golden conversation
    3. Emits events if found

    Args:
        scenario_name: Name of the scenario
        user_message: User's message
        run_id: Agent run ID
        thread_id: Thread ID
        owner_id: User ID
        message_id: Client message ID
        trace_id: Trace ID

    Returns:
        True if replay was executed, False if no matching conversation
    """
    if not is_replay_enabled():
        return False

    try:
        service = ReplayService(scenario_name)
        conv_id = service.match_conversation(user_message)

        if not conv_id:
            logger.info(f"No matching replay conversation for: {user_message[:50]}...")
            return False

        await service.emit_conversation_events(
            conversation_id=conv_id,
            run_id=run_id,
            thread_id=thread_id,
            owner_id=owner_id,
            message_id=message_id,
            trace_id=trace_id,
        )
        return True

    except FileNotFoundError:
        logger.warning(f"Scenario not found: {scenario_name}")
        return False
    except Exception as e:
        logger.exception(f"Replay error: {e}")
        return False


__all__ = [
    "ReplayService",
    "is_replay_enabled",
    "run_replay_conversation",
    "REPLAY_MODE_ENABLED",
]
