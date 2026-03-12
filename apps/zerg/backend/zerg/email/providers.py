"""Provider abstraction for *email* triggers.

The Gmail implementation delegates to connector-centric helpers in
``zerg.services.gmail_api`` and keeps the provider interface small so new
providers (Outlook, IMAP) can be added without bloating the core services.

The **registry pattern** keeps initialisation trivial: a global dictionary
maps the provider identifier (string) to a *singleton* provider instance.  We
expect the provider code to be *stateless* so a single instance per process
is sufficient.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from datetime import timezone
from email import policy
from email.header import decode_header
from email.header import make_header
from email.parser import BytesParser
from email.utils import getaddresses
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any
from typing import Protocol
from typing import runtime_checkable

from zerg.utils.log import log

# Structured logger (module-level) so helper methods can log without having
# to re-bind for every call.

logger = log.bind(component="gmail-provider")
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


@runtime_checkable
class EmailProvider(Protocol):
    """Minimal methods an email provider implementation must expose."""

    name: str  # human readable identifier, also used for metrics label

    async def process_trigger(self, trigger_id: int) -> None:  # noqa: D401 – async handler
        """Handle *one* trigger.

        The provider is responsible for:
        1. Any watch renewal logic (if applicable)
        2. Detecting new messages / events
        3. Publishing ``TRIGGER_FIRED`` and scheduling the run

        The signature keeps call-sites uniform across providers so the
        trigger scheduler can delegate without branching.
        """


# ---------------------------------------------------------------------------
# Gmail implementation – thin adapter around existing helpers
# ---------------------------------------------------------------------------


class GmailProvider:  # noqa: D101 – obvious from context
    """Concrete EmailProvider implementation for **Gmail**.

    The implementation was migrated from legacy trigger handling so that the full handling
    logic now lives inside the provider itself.  This removes the
    cross-service dependency and makes the call-sites uniform across all
    current and future providers.
    """

    name = "gmail"

    # A tiny in-memory cache that maps *refresh_token* → (access_token, expiry
    # epoch).  We purposely keep it **per-process** because webhook callbacks
    # and background services run in the same interpreter.
    _token_cache: dict[str, tuple[str, float]]

    def __init__(self) -> None:  # noqa: D401 – small helper
        # Instance is a singleton registered in `_REGISTRY` so we can safely
        # store mutable state here.
        self._token_cache = {}

    # ------------------------------------------------------------------
    # Watch renewal helpers --------------------------------------------
    # ------------------------------------------------------------------

    @staticmethod
    def _renew_watch_stub():  # noqa: D401 – tiny helper for dev/CI
        """Return new watch metadata identical to *watch* stub.

        The real implementation will call the Gmail *watch* API.  Until we
        wire that up, tests patch this helper for deterministic timestamps.
        """

        from datetime import datetime
        from datetime import timedelta
        from datetime import timezone

        now = datetime.now(tz=timezone.utc)
        return {
            "history_id": int(now.timestamp()),
            "watch_expiry": int((now + timedelta(days=7)).timestamp() * 1000),
        }

    async def _maybe_renew_watch(self, trg, session):  # noqa: D401 – small helper
        """Renew Gmail watch if expiry within next 24 h.

        Called from :meth:`process_trigger` **before** History diff so we keep
        the baseline up to date.  Only runs when the trigger has valid
        ``watch_expiry`` metadata – brand-new triggers are initialised
        elsewhere.
        """

        from datetime import datetime
        from datetime import timezone

        from zerg.metrics import gmail_api_error_total  # noqa: WPS433
        from zerg.metrics import gmail_watch_renew_total  # noqa: WPS433

        expiry_ts = (trg.config or {}).get("watch_expiry")  # milliseconds

        if expiry_ts is None:
            return  # no data yet – will be set by initialise helper

        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

        # Renew if expires within 24 h
        if expiry_ts - now_ms > 24 * 60 * 60 * 1000:
            return

        logger.info("renew-watch", trigger_id=trg.id)

        try:
            new_meta = await asyncio.to_thread(self._renew_watch_stub)
        except Exception as exc:  # pragma: no cover – unexpected stub failure
            logger.error("renew-watch-failed", trigger_id=trg.id, error=str(exc))
            gmail_api_error_total.inc()
            return

        cfg = trg.config or {}
        cfg.update(new_meta)
        trg.config = cfg  # type: ignore[assignment]

        try:
            from sqlalchemy.orm.attributes import flag_modified  # type: ignore

            flag_modified(trg, "config")
        except ImportError:
            pass

        session.add(trg)
        session.commit()

        gmail_watch_renew_total.inc()

    @staticmethod
    def _decode_header_value(value: str | None) -> str | None:
        if not value:
            return None
        try:
            return str(make_header(decode_header(value))).strip() or None
        except Exception:
            return value.strip() or None

    @staticmethod
    def _extract_addresses(message, header_name: str) -> tuple[str, ...]:
        raw_values = message.get_all(header_name, [])
        addresses: list[str] = []
        seen: set[str] = set()
        for _display, email_addr in getaddresses(raw_values):
            candidate = (email_addr or "").strip()
            if not candidate:
                continue
            lowered = candidate.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            addresses.append(candidate)
        return tuple(addresses)

    @classmethod
    def _html_to_text(cls, html_content: str | None) -> str:
        if not html_content:
            return ""
        stripped = _HTML_TAG_RE.sub(" ", html_content)
        return " ".join(unescape(stripped).split())

    @staticmethod
    def _get_part_text(part) -> str:
        try:
            content = part.get_content()
        except Exception:
            content = None

        if isinstance(content, str):
            return content
        if isinstance(content, bytes):
            charset = part.get_content_charset() or "utf-8"
            return content.decode(charset, errors="replace")

        payload = part.get_payload(decode=True)
        if payload:
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")

        raw_payload = part.get_payload()
        return raw_payload if isinstance(raw_payload, str) else ""

    @classmethod
    def _extract_body_text(cls, message, *, fallback: str | None = None) -> str:
        plain_parts: list[str] = []
        html_parts: list[str] = []

        for part in message.walk():
            if part.is_multipart():
                continue
            if (part.get_content_disposition() or "").lower() == "attachment":
                continue

            content_type = (part.get_content_type() or "").lower()
            text = cls._get_part_text(part).strip()
            if not text:
                continue
            if content_type == "text/plain":
                plain_parts.append(text)
            elif content_type == "text/html":
                html_parts.append(text)

        if plain_parts:
            return "\n\n".join(part for part in plain_parts if part).strip()
        if html_parts:
            html_text = "\n\n".join(part for part in html_parts if part).strip()
            text = cls._html_to_text(html_text)
            if text:
                return text

        return (fallback or "").strip()

    @staticmethod
    def _parse_sent_at(raw_message: dict[str, Any], parsed_message) -> datetime | None:
        internal_date = raw_message.get("internalDate")
        if internal_date:
            try:
                return datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)
            except Exception:
                pass

        date_header = parsed_message.get("Date")
        if not date_header:
            return None

        try:
            parsed = parsedate_to_datetime(date_header)
        except Exception:
            return None

        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _build_ingest_request(
        cls,
        *,
        owner_id: int,
        connector_id: int,
        mailbox_email: str | None,
        raw_message: dict[str, Any],
    ):
        from zerg.services.email_conversation_ingest import EmailConversationIngest

        raw_bytes = raw_message.get("raw_bytes")
        thread_id = str(raw_message.get("threadId") or "").strip()
        message_id = str(raw_message.get("id") or "").strip()
        if not raw_bytes or not thread_id or not message_id:
            return None

        parsed_message = BytesParser(policy=policy.default).parsebytes(raw_bytes)
        subject = cls._decode_header_value(parsed_message.get("Subject"))
        from_header = cls._decode_header_value(parsed_message.get("From"))
        from_emails = cls._extract_addresses(parsed_message, "From")
        reply_to_emails = cls._extract_addresses(parsed_message, "Reply-To")
        to_emails = cls._extract_addresses(parsed_message, "To")
        cc_emails = cls._extract_addresses(parsed_message, "Cc")
        fallback_text = raw_message.get("snippet") or subject or "(no content)"
        body_text = cls._extract_body_text(parsed_message, fallback=fallback_text)

        normalized_mailbox = (mailbox_email or "").strip().lower()
        is_outgoing = normalized_mailbox and any(addr.lower() == normalized_mailbox for addr in from_emails)
        provider_metadata = {
            "gmail_message_id": message_id,
            "thread_id": thread_id,
            "label_ids": list(raw_message.get("labelIds") or []),
            "history_id": raw_message.get("historyId"),
            "snippet": raw_message.get("snippet"),
            "rfc_message_id": cls._decode_header_value(parsed_message.get("Message-ID")),
            "references": cls._decode_header_value(parsed_message.get("References")),
            "in_reply_to": cls._decode_header_value(parsed_message.get("In-Reply-To")),
        }

        return EmailConversationIngest(
            owner_id=owner_id,
            connector_id=connector_id,
            provider="gmail",
            external_thread_id=thread_id,
            external_message_id=message_id,
            subject=subject,
            body_text=body_text,
            role="user",
            direction="outgoing" if is_outgoing else "incoming",
            sender_kind="human",
            sender_display=from_header or (from_emails[0] if from_emails else None),
            from_email=from_emails[0] if from_emails else None,
            reply_to_emails=reply_to_emails,
            to_emails=to_emails,
            cc_emails=cc_emails,
            raw_bytes=raw_bytes,
            raw_extension="eml",
            sent_at=cls._parse_sent_at(raw_message, parsed_message),
            provider_metadata=provider_metadata,
        )

    # ------------------------------------------------------------------
    # Public API (EmailProvider) ---------------------------------------
    # ------------------------------------------------------------------

    async def process_trigger(self, trigger_id: int) -> None:  # noqa: D401 – legacy helper (unused in connector-first mode)
        """Legacy entrypoint retained for compatibility.

        Looks up the trigger, extracts ``connector_id`` from its config and
        delegates to :meth:`process_connector` so we only maintain one code
        path. Triggers without a connector are ignored.
        """

        # ------------------------------------------------------------------
        # Measure end-to-end processing time for Prometheus  --------------
        # ------------------------------------------------------------------

        import time as _time_mod

        start_ts = _time_mod.perf_counter()

        # ------------------------------------------------------------------
        # Local imports to avoid import cycles (tests patch some internals)
        # ------------------------------------------------------------------

        # Re-load the trigger inside a fresh session so we can mutate JSON and
        # commit safely.
        from zerg.database import db_session  # local import to avoid cycles
        from zerg.models.models import Trigger  # noqa: WPS433

        with db_session() as session:
            trg: Trigger | None = session.query(Trigger).filter(Trigger.id == trigger_id).first()
            if not trg:
                logger.warning("trigger-missing", trigger_id=trigger_id)
                return
            cfg = trg.config or {}
            connector_id = cfg.get("connector_id")
            if connector_id is None:
                logger.debug("skip-no-connector", trigger_id=trigger_id)
                return
        await self.process_connector(int(connector_id))

        # ------------------------------------------------------------------
        # Prometheus – record overall latency
        # ------------------------------------------------------------------

        try:
            from zerg.metrics import trigger_processing_seconds  # noqa: WPS433

            trigger_processing_seconds.observe(_time_mod.perf_counter() - start_ts)
        except Exception:  # pragma: no cover – metrics disabled or import fail
            pass

    # ------------------------------------------------------------------
    # New entrypoint – process all triggers for a connector
    # ------------------------------------------------------------------

    async def process_connector(self, connector_id: int) -> None:
        """Fetch Gmail changes for a connector and apply all its triggers."""
        import time as _time_mod

        from sqlalchemy.orm.attributes import flag_modified  # type: ignore

        from zerg.database import db_session
        from zerg.events import EventType
        from zerg.events import event_bus
        from zerg.metrics import gmail_api_error_total
        from zerg.models.models import Connector as ConnectorModel
        from zerg.models.models import Trigger
        from zerg.services import email_filtering
        from zerg.services import gmail_api
        from zerg.services.email_conversation_ingest import EmailConversationIngestService
        from zerg.services.scheduler_service import scheduler_service
        from zerg.utils import crypto

        # keep perf counter available for potential future metrics
        # (unused in current implementation)

        with db_session() as session:
            conn: ConnectorModel | None = session.query(ConnectorModel).filter(ConnectorModel.id == connector_id).first()
            if not conn:
                logger.warning("connector-missing", connector_id=connector_id)
                return
            if conn.provider != "gmail" or conn.type != "email":
                logger.debug("connector-mismatch", connector_id=connector_id)
                return

            cfg = dict(conn.config or {})
            enc_token = cfg.get("refresh_token")
            if not enc_token:
                logger.warning("no-refresh-token", connector_id=connector_id)
                return

            refresh_token = crypto.decrypt(enc_token)

            # Access token with simple per-connector cache
            now = _time_mod.time()
            cached = self._token_cache.get(str(connector_id))
            if cached and cached[1] > now:
                access_token = cached[0]
            else:
                try:
                    access_token = await gmail_api.async_exchange_refresh_token(refresh_token)
                except Exception as exc:  # pragma: no cover
                    logger.error("refresh-token-exchange-failed", connector_id=connector_id, error=str(exc))
                    gmail_api_error_total.inc()
                    return
                self._token_cache[str(connector_id)] = (access_token, now + 55 * 60)

            # History diff at connector level
            start_hid = int(cfg.get("history_id", 0))
            history_records = await gmail_api.async_list_history(access_token, start_hid)
            if not history_records:
                logger.debug("history-empty", connector_id=connector_id)
                return

            # Flatten
            message_ids: list[str] = []
            max_hid = start_hid
            for h in history_records:
                try:
                    hid_int = int(h.get("id", 0))
                    max_hid = max(max_hid, hid_int)
                except Exception:
                    pass
                for added in h.get("messagesAdded", []):
                    mid = (added.get("message") or {}).get("id")
                    if mid:
                        message_ids.append(str(mid))

            if not message_ids:
                logger.debug("history-no-messages", connector_id=connector_id)
                if max_hid > start_hid:
                    cfg["history_id"] = max_hid
                    conn.config = cfg  # type: ignore[assignment]
                    try:
                        flag_modified(conn, "config")
                    except Exception:
                        pass
                    session.add(conn)
                    session.commit()
                return

            # Pre-fetch metadata per message once
            meta_cache: dict[str, dict] = {}
            for mid in message_ids:
                meta = await gmail_api.async_get_message_metadata(access_token, mid)
                if meta:
                    meta_cache[mid] = meta

            conversation_ingest = EmailConversationIngestService(session)
            ingested_total = 0
            mailbox_email = cfg.get("emailAddress")
            for mid in message_ids:
                raw_message = await gmail_api.async_get_message_raw(access_token, mid)
                if not raw_message:
                    continue
                ingest_request = self._build_ingest_request(
                    owner_id=conn.owner_id,
                    connector_id=connector_id,
                    mailbox_email=mailbox_email,
                    raw_message=raw_message,
                )
                if ingest_request is None:
                    logger.debug("conversation-ingest-skip", connector_id=connector_id, message_id=mid)
                    continue
                try:
                    conversation_ingest.ingest(ingest_request)
                    ingested_total += 1
                except Exception as exc:
                    logger.exception(
                        "conversation-ingest-failed",
                        connector_id=connector_id,
                        message_id=mid,
                        error=str(exc),
                    )

            # Load triggers referencing this connector
            triggers = [
                trg
                for trg in session.query(Trigger).filter(Trigger.type == "email").all()
                if (trg.config or {}).get("connector_id") == connector_id
            ]

            fired_total = 0
            for trg in triggers:
                filters = (trg.config or {}).get("filters")
                for mid, meta in meta_cache.items():
                    if not email_filtering.matches(meta, filters):
                        continue
                    await event_bus.publish(
                        EventType.TRIGGER_FIRED,
                        {
                            "trigger_id": trg.id,
                            "fiche_id": trg.fiche_id,
                            "provider": "gmail",
                            "message_id": mid,
                            "trigger_type": "webhook",
                        },
                    )
                    await scheduler_service.run_fiche_task(trg.fiche_id, trigger="webhook")  # type: ignore[arg-type]
                    fired_total += 1

            # Update connector history id
            if max_hid > start_hid:
                cfg["history_id"] = max_hid
                conn.config = cfg  # type: ignore[assignment]
                try:
                    flag_modified(conn, "config")
                    # Update metrics
                    from zerg.metrics import gmail_connector_history_id

                    gmail_connector_history_id.labels(
                        connector_id=str(connector_id),
                        owner_id=str(conn.owner_id),
                    ).set(max_hid)
                except Exception:
                    pass
                session.add(conn)
                session.commit()

            logger.info(
                "connector-processed",
                connector_id=connector_id,
                messages=len(message_ids),
                ingested=ingested_total,
                fired=fired_total,
            )


# ---------------------------------------------------------------------------
# Outlook placeholder – keeps tests & type-checkers happy
# ---------------------------------------------------------------------------


class OutlookProvider:  # noqa: D101 – placeholder, raises for now
    name = "outlook"

    async def process_trigger(self, trigger_id: int) -> None:  # noqa: D401 – interface compliance
        raise NotImplementedError("Outlook provider not implemented yet")


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, EmailProvider] = {
    "gmail": GmailProvider(),
    "outlook": OutlookProvider(),
}


def get_provider(name: str) -> EmailProvider | None:  # noqa: D401 – tiny helper
    """Return provider instance or *None* if unsupported."""

    return _REGISTRY.get(name)


# ---------------------------------------------------------------------------
# Convenience for logging / debug prints
# ---------------------------------------------------------------------------


def list_supported() -> list[str]:  # noqa: D401 – tiny helper
    """Return list of provider identifiers registered."""

    return list(_REGISTRY)


# Keep a conventional *logging.Logger* around for third-party modules that
# expect the classic logging interface.
