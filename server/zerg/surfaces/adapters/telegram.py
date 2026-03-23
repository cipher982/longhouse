"""Telegram implementation of the SurfaceAdapter contract."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING
from typing import Any

from zerg.surfaces.base import SurfaceInboundEvent

if TYPE_CHECKING:
    from zerg.surfaces.base import SurfaceMode

_UNRESOLVED_OWNER_MESSAGE = (
    "Hi! I'm your Longhouse assistant.\n\n"
    "To link this chat to your account, run:\n"
    "<code>/link YOUR_TOKEN</code>\n\n"
    "Get your token from <b>Settings → Telegram</b> in the Longhouse web app."
)


class TelegramSurfaceAdapter:
    """Adapter that maps Telegram events to the shared orchestrator contract."""

    surface_id = "telegram"
    mode: SurfaceMode = "push"

    def __init__(
        self,
        *,
        send_cb: Callable[[str, str, str | None], Awaitable[None]],
        resolve_owner_cb: Callable[[str], Awaitable[int | None]],
        persist_chat_id_cb: Callable[[int, str], Awaitable[None]],
        formatter: Callable[[str], str],
    ) -> None:
        self._send_cb = send_cb
        self._resolve_owner_cb = resolve_owner_cb
        self._persist_chat_id_cb = persist_chat_id_cb
        self._formatter = formatter

    async def normalize_inbound(self, raw_input: Any) -> SurfaceInboundEvent | None:
        event = raw_input if isinstance(raw_input, dict) else {}
        text = (event.get("text") or "").strip()
        if not text:
            return None

        raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
        chat_id = str(event.get("chat_id", "") or "")
        thread_id = str(event.get("thread_id", "") or raw.get("thread_id", "") or "").strip()
        update_id = str(raw.get("update_id", "") or "")
        dedupe_key = f"telegram:{chat_id}:{update_id}" if chat_id and update_id else ""
        source_message_id = str(event.get("message_id", "") or "") or None

        raw_payload = dict(raw)
        if "chat_type" not in raw_payload and event.get("chat_type"):
            raw_payload["chat_type"] = event.get("chat_type")
        if thread_id:
            raw_payload["thread_id"] = thread_id
        if event.get("reply_to_id") and "reply_to_id" not in raw_payload:
            raw_payload["reply_to_id"] = event.get("reply_to_id")

        if chat_id:
            conversation_id = f"telegram:{chat_id}:topic:{thread_id}" if thread_id else f"telegram:{chat_id}"
        else:
            conversation_id = "telegram:"

        return SurfaceInboundEvent(
            surface_id=self.surface_id,
            conversation_id=conversation_id,
            dedupe_key=dedupe_key,
            owner_hint=chat_id or None,
            source_message_id=source_message_id,
            source_event_id=update_id or None,
            text=text,
            timestamp_utc=datetime.now(timezone.utc),
            raw=raw_payload,
        )

    async def resolve_owner_id(self, event: SurfaceInboundEvent, _db) -> int | None:
        chat_id = self._chat_id(event)
        if not chat_id:
            return None

        owner_id = await self._resolve_owner_cb(chat_id)
        if owner_id is None:
            return None

        chat_type = str((event.raw or {}).get("chat_type", "") or "")
        if chat_type == "dm":
            await self._persist_chat_id_cb(owner_id, chat_id)

        return owner_id

    def build_run_kwargs(self, _event: SurfaceInboundEvent) -> dict[str, Any]:
        return {
            "timeout": 120,
            "return_on_deferred": False,
        }

    async def deliver(self, *, owner_id: int, text: str, event: SurfaceInboundEvent) -> None:
        del owner_id
        chat_id = self._chat_id(event)
        if not chat_id:
            raise ValueError("missing telegram chat_id for delivery")
        thread_id = str((event.raw or {}).get("thread_id", "") or "").strip() or None
        await self._send_cb(chat_id, self._formatter(text or "Done."), thread_id)

    async def handle_unresolved_owner(self, event: SurfaceInboundEvent) -> None:
        chat_id = self._chat_id(event)
        if not chat_id:
            return
        thread_id = str((event.raw or {}).get("thread_id", "") or "").strip() or None
        await self._send_cb(chat_id, _UNRESOLVED_OWNER_MESSAGE, thread_id)

    @staticmethod
    def _chat_id(event: SurfaceInboundEvent) -> str:
        if event.owner_hint:
            return str(event.owner_hint)
        if event.conversation_id.startswith("telegram:"):
            return event.conversation_id.split(":", 1)[1]
        return ""
