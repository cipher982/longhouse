"""Telegram ↔ Oikos bridge.

Routes inbound Telegram messages to OikosService and sends replies back.

Identity resolution:
- Single-tenant (SINGLE_TENANT=1): all messages route to the owner user (first ADMIN, else first user)
- Multi-tenant: looks up user.context['telegram_chat_id'] for the sender
- Unknown senders in multi-tenant mode receive a /link prompt

Account linking (multi-tenant):
- User generates a token in Settings → Telegram (stored in user.context['telegram_link_token'])
- User sends /link <token> to the bot
- Bridge validates token, writes telegram_chat_id to user.context, consumes the token
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING
from typing import Callable

from zerg.config import get_settings
from zerg.database import db_session

if TYPE_CHECKING:
    from zerg.channels.plugins.telegram import TelegramChannel
    from zerg.channels.types import ChannelMessage
    from zerg.channels.types import ChannelMessageEvent

logger = logging.getLogger(__name__)

# How long to poll for typing indicators while oikos is running (seconds)
_TYPING_INTERVAL = 4.0


class TelegramBridge:
    """Bridge between TelegramChannel and OikosService.

    Lifecycle:
        bridge = TelegramBridge(channel)
        bridge.start()   # subscribe to inbound messages
        # ... app runs ...
        bridge.stop()    # unsubscribe
    """

    def __init__(self, channel: "TelegramChannel") -> None:
        self._channel = channel
        self._unsubscribe: Callable[[], None] | None = None

    def start(self) -> None:
        """Subscribe to inbound Telegram messages."""
        self._unsubscribe = self._channel.on_message(self._on_message)
        logger.info("TelegramBridge: started")

    def stop(self) -> None:
        """Unsubscribe from inbound messages."""
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        logger.info("TelegramBridge: stopped")

    # --- Callbacks ---

    def _on_message(self, event: "ChannelMessageEvent") -> None:
        """Sync callback from channel – schedules async handling."""
        asyncio.create_task(self._handle_message(event))

    async def _handle_message(self, event: "ChannelMessageEvent") -> None:
        """Process one inbound Telegram message end-to-end."""
        chat_id = event.get("chat_id", "")
        text = (event.get("text") or "").strip()

        if not text:
            return

        # /link command: associate Telegram chat with a Longhouse account
        if text.lower().startswith("/link"):
            await self._handle_link_command(chat_id, text)
            return

        # /start command: friendly greeting
        if text.lower().startswith("/start"):
            await self._send(chat_id, _WELCOME_MESSAGE)
            return

        # Resolve to a Longhouse user
        owner_id = await self._resolve_user(chat_id)
        if owner_id is None:
            await self._send(
                chat_id,
                (
                    "Hi! I'm your Longhouse assistant.\n\n"
                    "To link this chat to your account, run:\n"
                    "<code>/link YOUR_TOKEN</code>\n\n"
                    "Get your token from <b>Settings → Telegram</b> in the Longhouse web app."
                ),
            )
            return

        # Persist chat_id from DMs only — prevents group messages from
        # overwriting the user's real chat_id (and hijacking notifications)
        if event.get("chat_type") == "dm":
            await self._persist_chat_id(owner_id, chat_id)

        idempotency_key = self._build_idempotency_key(chat_id, event)
        if idempotency_key and await self._is_duplicate_inbound(owner_id, idempotency_key):
            logger.info("TelegramBridge: deduped retry for chat %s key %s", chat_id, idempotency_key)
            return

        # Send typing indicator and keep refreshing it while Oikos runs
        typing_task = asyncio.create_task(self._keep_typing(chat_id))
        try:
            raw = event.get("raw") or {}
            source_message_id = str(event.get("message_id", "") or "")
            source_event_id = str(raw.get("update_id", "") or "")
            result_text = await self._run_oikos(
                owner_id,
                text,
                chat_id=chat_id,
                source_message_id=source_message_id or None,
                source_event_id=source_event_id or None,
                source_idempotency_key=idempotency_key,
            )
        except Exception as e:
            logger.exception(f"TelegramBridge: oikos failed for chat {chat_id}: {e}")
            result_text = "Sorry, I ran into an error. Please try again."
        finally:
            typing_task.cancel()

        await self._send(chat_id, _format_for_telegram(result_text or "Done."))

    # --- Oikos execution ---

    async def _run_oikos(
        self,
        owner_id: int,
        task: str,
        *,
        chat_id: str,
        source_message_id: str | None = None,
        source_event_id: str | None = None,
        source_idempotency_key: str | None = None,
    ) -> str | None:
        """Run OikosService and return the text result."""
        from zerg.services.oikos_service import OikosService

        with db_session() as db:
            service = OikosService(db)
            result = await service.run_oikos(
                owner_id=owner_id,
                task=task,
                timeout=120,
                return_on_deferred=False,
                source_surface_id="telegram",
                source_conversation_id=f"telegram:{chat_id}",
                source_message_id=source_message_id,
                source_event_id=source_event_id,
                source_idempotency_key=source_idempotency_key,
            )
        return result.result

    def _build_idempotency_key(self, chat_id: str, event: "ChannelMessageEvent") -> str | None:
        """Build a stable key for Telegram webhook retry dedupe."""
        raw = event.get("raw") or {}
        update_id = str(raw.get("update_id", "") or "")
        if update_id:
            return f"telegram:{chat_id}:{update_id}"
        message_id = str(event.get("message_id", "") or "")
        if message_id:
            return f"telegram:{chat_id}:message:{message_id}"
        return None

    async def _is_duplicate_inbound(self, owner_id: int, idempotency_key: str) -> bool:
        """Return True if this inbound Telegram message was already persisted."""
        if not idempotency_key:
            return False

        from zerg.models.thread import ThreadMessage
        from zerg.services.oikos_service import OikosService

        try:
            with db_session() as db:
                service = OikosService(db)
                fiche = service.get_or_create_oikos_fiche(owner_id)
                thread = service.get_or_create_oikos_thread(owner_id, fiche)
                rows = (
                    db.query(ThreadMessage.message_metadata)
                    .filter(
                        ThreadMessage.thread_id == thread.id,
                        ThreadMessage.role == "user",
                    )
                    .order_by(ThreadMessage.id.desc())
                    .limit(500)
                    .all()
                )
                for row in rows:
                    metadata = row[0] or {}
                    surface = metadata.get("surface") if isinstance(metadata, dict) else {}
                    if isinstance(surface, dict) and surface.get("idempotency_key") == idempotency_key:
                        return True
        except Exception:
            logger.warning(
                "TelegramBridge: dedupe lookup failed for owner=%s key=%s; continuing without dedupe",
                owner_id,
                idempotency_key,
                exc_info=True,
            )
        return False

    # --- Identity resolution ---

    async def _resolve_user(self, telegram_chat_id: str) -> int | None:
        """Map a Telegram chat_id to a Longhouse user ID.

        Single-tenant: returns the first ADMIN user (or first user if no admin).
        Multi-tenant: scans user.context['telegram_chat_id'].
        """
        from zerg.models.user import User as UserModel

        settings = get_settings()

        with db_session() as db:
            if settings.single_tenant:
                # Route all messages to the owner (first ADMIN, else first user)
                admin = db.query(UserModel).filter(UserModel.role == "ADMIN").first()
                if admin:
                    return admin.id
                first = db.query(UserModel).first()
                return first.id if first else None

            # Multi-tenant: look up by stored chat_id
            users = db.query(UserModel).all()
            for user in users:
                ctx = user.context or {}
                if str(ctx.get("telegram_chat_id", "")) == str(telegram_chat_id):
                    return user.id

        return None

    # --- Account linking ---

    async def _handle_link_command(self, chat_id: str, text: str) -> None:
        """Handle /link <token> command."""
        parts = text.strip().split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await self._send(
                chat_id,
                "Usage: <code>/link YOUR_TOKEN</code>\n\nGet your token from <b>Settings → Telegram</b>.",
            )
            return

        token = parts[1].strip()
        success = await self._link_account(chat_id, token)
        if success:
            await self._send(chat_id, "✓ Account linked! You can now chat with your Longhouse assistant.")
        else:
            await self._send(chat_id, "Invalid or expired token. Please generate a fresh one in <b>Settings → Telegram</b>.")

    async def _link_account(self, telegram_chat_id: str, token: str) -> bool:
        """Validate token and write telegram_chat_id to the matching user's context."""
        from zerg.models.user import User as UserModel

        with db_session() as db:
            users = db.query(UserModel).all()
            for user in users:
                ctx = dict(user.context or {})
                if ctx.get("telegram_link_token") == token:
                    ctx["telegram_chat_id"] = str(telegram_chat_id)
                    ctx.pop("telegram_link_token", None)  # consume token
                    user.context = ctx
                    db.commit()
                    logger.info(f"TelegramBridge: linked chat {telegram_chat_id} → user {user.id}")
                    return True
        return False

    # --- Helpers ---

    async def _persist_chat_id(self, owner_id: int, telegram_chat_id: str) -> None:
        """Store telegram_chat_id in user.context so Oikos tools can reach the user."""
        from zerg.models.user import User as UserModel

        with db_session() as db:
            user = db.query(UserModel).filter(UserModel.id == owner_id).first()
            if user and str((user.context or {}).get("telegram_chat_id", "")) != str(telegram_chat_id):
                ctx = dict(user.context or {})
                ctx["telegram_chat_id"] = str(telegram_chat_id)
                user.context = ctx
                db.commit()
                logger.info("TelegramBridge: stored chat_id %s for user %s", telegram_chat_id, owner_id)

    async def _send(self, chat_id: str, text: str) -> None:
        """Send an HTML-formatted message to a Telegram chat."""
        msg: ChannelMessage = {
            "channel_id": "telegram",
            "to": chat_id,
            "text": text,
            "parse_mode": "html",
        }
        result = await self._channel.send_message(msg)
        if not result.get("success"):
            logger.warning(
                "TelegramBridge: send failed to chat %s: %s (code=%s)",
                chat_id,
                result.get("error"),
                result.get("error_code"),
            )

    async def _keep_typing(self, chat_id: str) -> None:
        """Send typing action repeatedly until cancelled."""
        try:
            while True:
                await self._channel.send_typing(chat_id)
                await asyncio.sleep(_TYPING_INTERVAL)
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Markdown → Telegram HTML formatting
# ---------------------------------------------------------------------------

_WELCOME_MESSAGE = (
    "Hi! I'm your <b>Longhouse</b> assistant.\n\n"
    "Just send me a message and I'll get to work.\n\n"
    "Commands:\n"
    "• /start — show this message\n"
    "• /link &lt;token&gt; — link to your account (multi-tenant only)"
)


def _format_for_telegram(text: str) -> str:
    """Convert LLM markdown output to Telegram HTML.

    Handles the most common patterns: code blocks, inline code, bold, italic.
    Strips unsupported markdown rather than letting raw symbols render.

    Must be called before HTML-escaping the plain parts to avoid double-escaping.
    The approach: extract code spans first (preserve literally), escape the rest,
    then inject back.
    """
    # --- Step 1: extract code blocks and inline code before escaping ---
    placeholders: list[str] = []

    def _stash(match: re.Match) -> str:
        placeholders.append(match.group(0))
        return f"\x00{len(placeholders) - 1}\x00"

    # Fenced code blocks (``` ... ```)
    text = re.sub(r"```(?:[\w+-]*)?\n?(.*?)```", _stash, text, flags=re.DOTALL)
    # Inline code
    text = re.sub(r"`([^`\n]+)`", _stash, text)

    # --- Step 2: HTML-escape the non-code parts ---
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # --- Step 3: apply inline formatting ---
    # Bold: **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.DOTALL)
    # Italic: *text* or _text_ (word-boundary aware)
    text = re.sub(r"\*([^*\n]+)\*", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<i>\1</i>", text)
    # Strikethrough: ~~text~~
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # --- Step 4: restore code placeholders (HTML-escape content before wrapping) ---
    def _restore(match: re.Match) -> str:
        idx = int(match.group(1))
        raw = placeholders[idx]
        # Was it a fenced block or inline?
        if raw.startswith("```"):
            inner = re.sub(r"^```(?:[\w+-]*)?\n?", "", raw)
            # Strip trailing fence exactly (don't rstrip backticks from content)
            if inner.endswith("```"):
                inner = inner[:-3]
            inner = inner.rstrip("\n")
        else:
            # Strip exactly one leading and trailing backtick
            inner = raw[1:-1] if raw.startswith("`") and raw.endswith("`") else raw

        # HTML-escape code content so < > & don't break Telegram HTML parser
        inner = inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        tag = "pre" if raw.startswith("```") else "code"
        return f"<{tag}>{inner}</{tag}>"

    text = re.sub(r"\x00(\d+)\x00", _restore, text)

    return text
