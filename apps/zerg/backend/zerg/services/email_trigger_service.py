"""Background coroutine that polls *email* triggers.

Phase-1 only implements the scaffolding so the new *email* trigger type is
fully wired into the application lifecycle.  Actual IMAP connectivity and
filtering will be implemented in a follow-up milestone.

The service follows the same start/stop interface as ``SchedulerService`` so
`zerg.main` can manage both identically.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from zerg.database import db_session
from zerg.database import get_session_factory
from zerg.email.providers import get_provider
from zerg.metrics import gmail_api_error_total
from zerg.metrics import gmail_watch_renew_total
from zerg.models.models import Trigger
from zerg.models.models import User

# HTTP helpers
# Structured logger wrapper
from zerg.utils.log import log

# Use structured logging directly


class EmailTriggerService:
    """Very lightweight polling loop for *email* triggers (stub version).

    The loop periodically checks the database for any triggers of type
    ``email``.  For **now** it only outputs a warning if such a trigger exists
    because the actual IMAP handling has not yet landed.
    """

    _instance: "EmailTriggerService | None" = None

    def __new__(cls):
        # Force singleton so the global ``email_trigger_service`` behaves
        # similar to ``scheduler_service`` in the codebase.
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, session_factory: Optional["sessionmaker"] = None):
        # Lazily initialise the DB session factory so callers (main app or
        # tests) can inject a custom one.  Falling back to
        # ``get_session_factory()`` removes the last implicit dependency on
        # the *default_session_factory* global which we plan to deprecate.

        self._session_factory = session_factory or get_session_factory()

        self._task: Optional[asyncio.Task[None]] = None
        self._shutdown_event = asyncio.Event()

    async def start(self):
        if self._task is not None:
            log.debug("email-trigger", event="already-running")
            return

        self._shutdown_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        log.info("email-trigger", event="started", mode="stub")

    async def stop(self):
        if self._task is None:
            return

        self._shutdown_event.set()
        # Cancel the task after the event is set in case it's waiting on sleep.
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        log.info("email-trigger", event="stopped")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_loop(self):
        """Main polling coroutine.

        Runs until ``_shutdown_event`` is set.
        """

        poll_interval_s = 600  # 10-minute safety-net poll; configurable via env later

        while not self._shutdown_event.is_set():
            try:
                await self._check_email_triggers()
            except Exception as exc:  # pragma: no cover – don't crash service
                log.exception("email-trigger", event="loop-error", error=str(exc))

            # Wait for either shutdown event or next poll interval
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=poll_interval_s)
            except asyncio.TimeoutError:
                # Normal – loop again
                pass

    async def _check_email_triggers(self):
        """Process *email* triggers.

        Current responsibilities per trigger:
        1. Renew Gmail watches that are about to expire.
        2. Perform lightweight polling (token refresh), stub.
        3. (Future) IMAP / provider-specific checks.
        """

        # Fetch trigger data with extracted config to avoid DetachedInstanceError
        def _db_query() -> list[dict]:
            with db_session(self._session_factory) as session:
                db_triggers = session.query(Trigger).filter(Trigger.type == "email").all()
                # Extract all needed data while still in session context
                return [
                    {
                        "id": trg.id,
                        "config": trg.config or {},
                        "agent_id": trg.agent_id,
                        "type": trg.type,
                        # Add other fields as needed
                    }
                    for trg in db_triggers
                ]

        trigger_data = await asyncio.to_thread(_db_query)

        if not trigger_data:
            return

        for trg_data in trigger_data:
            provider_name = trg_data["config"].get("provider", "gmail")

            # ------------------------------------------------------------------
            # Delegate provider-specific handling
            # ------------------------------------------------------------------

            provider_impl = get_provider(str(provider_name))

            if provider_impl is None:
                log.warning(
                    "email-trigger", event="unsupported-provider", provider=provider_name, trigger_id=trg_data["id"]
                )
                continue

            # Gmail logic now lives **entirely** in ``GmailProvider``.  The
            # deprecated helper ``_handle_gmail_trigger`` has been turned into
            # a no-op stub so we can safely skip it here.  Watch renewal logic
            # is still implemented below because it has not yet been ported to
            # the provider abstraction.

            # Renew watch logic is Gmail-specific and currently lives as a
            # private method.  Call it directly here to avoid altering the
            # established tests for now.  For non-Gmail providers we expect
            # them to handle renewal inside ``process_trigger``.

            # GmailProvider now handles watch renewal internally – no need
            # to call the legacy helper here.

            # Finally hand over to provider implementation ----------------------------------------------------

            try:
                await provider_impl.process_trigger(trg_data["id"])
            except NotImplementedError:
                log.info(
                    "email-trigger", event="provider-not-implemented", provider=provider_name, trigger_id=trg_data["id"]
                )
            except Exception as exc:  # pragma: no cover – safety net
                log.exception(
                    "email-trigger",
                    event="provider-error",
                    provider=provider_name,
                    trigger_id=trg_data["id"],
                    error=str(exc),
                )

    # Note: Deprecated _handle_gmail_trigger method removed - functionality moved to GmailProvider

    # ------------------------------------------------------------------
    # Static utility ----------------------------------------------------
    # ------------------------------------------------------------------

    # `_exchange_refresh_token` was moved to `zerg.services.gmail_api` so the
    # logic can be re-used by other services.  The old helper is deleted to
    # avoid duplicate code paths.

    # ------------------------------------------------------------------
    # Watch renewal helpers --------------------------------------------
    # ------------------------------------------------------------------

    async def _maybe_renew_gmail_watch(self, trigger):  # noqa: D401 – short name
        """Renew Gmail watch if expiry is within 24 h.

        We operate fully with **stub** data when running under the test
        suite or dev mode so no external network traffic is generated.  The
        method therefore delegates to ``_renew_gmail_watch_stub`` which
        returns new history / expiry values.
        """

        cfg = trigger.config or {}
        expiry_ts = cfg.get("watch_expiry")  # milliseconds since epoch

        if expiry_ts is None:
            # Cannot decide – trigger likely never initialised yet
            return

        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

        # Renew if expiry within next 24 hours (86400*1000 ms)
        if expiry_ts - now_ms > 24 * 60 * 60 * 1000:
            return  # still valid

        # Renew ----------------------------------------------------------
        log.info("email-trigger", event="renew-gmail-watch", trigger_id=trigger.id)

        try:
            new_watch = await asyncio.to_thread(self._renew_gmail_watch_stub)
        except Exception as exc:  # pragma: no cover
            log.error("email-trigger", event="renew-gmail-watch-failed", error=str(exc))
            # Metrics ---------------------------------------------------
            gmail_api_error_total.inc()
            return

        cfg.update(new_watch)
        trigger.config = cfg  # type: ignore[assignment]

        # Metrics -------------------------------------------------------
        gmail_watch_renew_total.inc()

        try:
            from sqlalchemy.orm.attributes import flag_modified  # type: ignore

            flag_modified(trigger, "config")
        except ImportError:  # pragma: no cover
            pass

        # Persist
        with db_session(self._session_factory) as session:
            session.merge(trigger)

    @staticmethod
    def _renew_gmail_watch_stub():
        """Return new watch meta – identical to *start* stub."""

        now = datetime.now(tz=timezone.utc)
        return {
            "history_id": int(now.timestamp()),
            "watch_expiry": int((now + timedelta(days=7)).timestamp() * 1000),
        }

    # ------------------------------------------------------------------
    # Public helpers called by router hooks -----------------------------
    # ------------------------------------------------------------------

    async def initialize_gmail_trigger(self, db_session: Session, trigger):  # noqa: D401 – short name
        """Ensure Gmail *watch* registration & baseline history id.

        This helper is invoked right after an *email* trigger of provider
        **gmail** is created.  The logic is deliberately very light-weight for
        now – we *do not* perform the network request to Google by default so
        CI remains free of external dependencies.  Instead we:

        1. Look up any user who has a ``gmail_refresh_token`` (dev/CI only has
           one user row).  In the future triggers will be linked to a specific
           user.
        2. If the trigger config already contains ``history_id`` we assume the
           watch is active and do nothing.
        3. Otherwise we generate a **dummy** history_id plus an expiry that is
           7 days in the future.  Tests can monkey-patch the private helper
           ``_start_gmail_watch`` to return deterministic values.
        4. Persist the updated config JSON on the trigger row.

        A follow-up milestone will replace step 3 with a real call to the
        *watch* endpoint and will store the returned ``historyId`` &
        ``expiration``.
        """

        if (trigger.config or {}).get("history_id") is not None:
            log.debug("email-trigger", event="gmail-watch-already-exists", trigger_id=trigger.id)
            return

        # Pick *any* user with gmail refresh token for MVP ----------------
        user: User | None = db_session.query(User).filter(User.gmail_refresh_token.isnot(None)).first()

        if user is None:
            log.warning("email-trigger", event="no-gmail-user", trigger_id=trigger.id)
            return  # Cannot proceed – will try later when service runs

        # ------------------------------------------------------------------
        # In production we would obtain an access_token and call Gmail here.
        # The call is encapsulated in a private helper so tests can patch it.
        # ------------------------------------------------------------------

        try:
            watch_info = await asyncio.to_thread(self._start_gmail_watch_stub)
        except Exception as exc:  # pragma: no cover – network failure etc.
            log.error("email-trigger", event="gmail-watch-start-failed", trigger_id=trigger.id, error=str(exc))
            return

        # Merge into trigger.config ----------------------------------------
        cfg = dict(trigger.config or {})
        cfg.update(
            {
                "history_id": watch_info["history_id"],
                "watch_expiry": watch_info["watch_expiry"],
            }
        )
        trigger.config = cfg  # type: ignore[assignment]

        # Flag JSON column as modified so SQLAlchemy persists it
        try:
            from sqlalchemy.orm.attributes import flag_modified  # type: ignore

            flag_modified(trigger, "config")
        except ImportError:  # pragma: no cover
            pass

        db_session.add(trigger)
        db_session.commit()

    # ------------------------------------------------------------------
    # Stub for Gmail watch registration --------------------------------
    # ------------------------------------------------------------------

    @staticmethod
    def _start_gmail_watch_stub():
        """Return dummy watch metadata (for dev / CI).

        The real implementation will contact the Gmail API – this stub keeps
        unit-tests self-contained and allows monkey-patching.
        """

        now = datetime.now(tz=timezone.utc)
        return {
            "history_id": 1,
            "watch_expiry": int((now + timedelta(days=7)).timestamp() * 1000),
        }


# Public singleton, mirroring ``scheduler_service`` pattern.
email_trigger_service = EmailTriggerService()
