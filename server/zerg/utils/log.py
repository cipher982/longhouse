"""Minimal wrapper that prefers *structlog* but gracefully falls back to the
standard ``logging`` module when the dependency is not installed.

Why this wrapper?
-----------------
* We want structured JSON logs in production (``structlog``).
* Unit-tests and local dev environments might not have the extra package.
* The rest of the codebase can therefore *always* ``from zerg.utils.log import log``
  and use ``log.info("msg", key=value)`` – calls behave the same regardless
  of whether structlog is available.
"""

from __future__ import annotations

import logging
from typing import Any


def _make_fallback_logger() -> "logging.Logger":  # noqa: D401 – small helper
    """Return a std-lib logger configured for dev/tests."""

    logger = logging.getLogger("zerg")

    if not logger.handlers:
        # BasicConfig is a no-op if already configured – run only once.
        logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")

    return logger


# Try structlog first
try:
    import structlog  # type: ignore

    # Keep the logger global so every import shares the same base instance.
    log = structlog.get_logger("zerg")  # type: ignore[invalid-name]

    if not structlog.is_configured():
        structlog.configure(processors=[structlog.processors.JSONRenderer()])

    if not hasattr(log, "bind"):

        class _StructAdapter:
            def __init__(self, base):
                self._base = base

            def bind(self, **_kw):
                return self

            def __getattr__(self, name):
                return getattr(self._base, name)

        log = _StructAdapter(log)  # type: ignore[assignment, invalid-name]

except ModuleNotFoundError:

    class _StdLoggerAdapter:
        """Thin adapter so code can call ``.bind()`` like with structlog."""

        def __init__(self, base: logging.Logger):
            self._base = base

        # structlog-compatible API ------------------------------------------------

        def bind(self, **_kw):  # noqa: D401 – API shim
            return self  # Ignore bindings for std logging

        # Delegate common log methods -------------------------------------------

        def _fmt(self, msg: str, kw) -> str:  # noqa: D401 – helper
            return f"{msg} {kw}" if kw else msg

        def debug(self, msg: str, *args, **kw):  # noqa: D401 – keep parity
            if args:
                msg = msg % args  # mimic printf style for legacy calls
            self._base.debug(self._fmt(msg, kw))

        def info(self, msg: str, *args, **kw):
            if args:
                msg = msg % args
            self._base.info(self._fmt(msg, kw))

        def warning(self, msg: str, *args, **kw):
            if args:
                msg = msg % args
            self._base.warning(self._fmt(msg, kw))

        def error(self, msg: str, *args, **kw):
            if args:
                msg = msg % args
            self._base.error(self._fmt(msg, kw))

        def exception(self, msg: str, *args, **kw):
            if args:
                msg = msg % args
            self._base.exception(self._fmt(msg, kw))

    log = _StdLoggerAdapter(_make_fallback_logger())  # type: ignore[invalid-name]


def get_logger(**bindings: Any):  # noqa: D401 – factory helper
    """Return a child/bound logger with optional key/value bindings."""

    import importlib.util

    if importlib.util.find_spec("structlog") is not None:
        return log.bind(**bindings)  # type: ignore[attr-defined]
    else:
        return log


class BestEffortLogger:
    """Rate-limited logger for long-running best-effort loops.

    Background threads (transcript tailers, cleanup loops) must never crash
    the process, but they also must never fail silently — a thread that
    swallows the same exception every poll for an entire session is how
    "steerable but zero timeline messages" hides for weeks.

    Usage::

        bf = BestEffortLogger("zerg.cursor_helm.ingest")
        try:
            ...  # one poll iteration
            bf.success()
        except Exception as exc:  # noqa: BLE001 - best-effort
            bf.failure("transcript poll", exc)

    - First failure logs WARNING immediately (so it is visible without --verbose).
    - Subsequent failures log every ``every`` attempts (noisy-but-not-flooded).
    - ``success()`` after failures logs INFO once ("recovered after N failures").
    - ``consecutive_failures`` is exposed so callers can write ingest health to
      state files / runtime signals.
    """

    def __init__(self, name: str, *, every: int = 10) -> None:
        self._log = get_logger(component=name)
        self._every = max(1, every)
        self._failures = 0
        self._total_failures = 0
        self._total_successes = 0
        self._last_error: str | None = None

    def failure(self, context: str, exc: BaseException) -> None:
        self._failures += 1
        self._total_failures += 1
        self._last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        if self._failures == 1 or self._failures % self._every == 0:
            self._log.warning(
                "best-effort failure",
                context=context,
                attempt=self._failures,
                error=self._last_error,
            )

    def success(self) -> None:
        self._total_successes += 1
        if self._failures > 0:
            self._log.info("best-effort recovered", context="transcript", after_failures=self._failures)
        self._failures = 0
        self._last_error = None

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    @property
    def total_failures(self) -> int:
        return self._total_failures

    @property
    def total_successes(self) -> int:
        return self._total_successes

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def summarize(self, context: str) -> None:
        """Emit one end-of-run summary line. Logs WARNING if the loop ended
        while degraded (consecutive failures at exit), else INFO. Safe to call
        once after the loop's stop event has been set."""
        if self._failures > 0:
            self._log.warning(
                "best-effort summary",
                context=context,
                consecutive_failures=self._failures,
                total_failures=self._total_failures,
                total_successes=self._total_successes,
                last_error=self._last_error,
            )
        else:
            self._log.info(
                "best-effort summary",
                context=context,
                total_failures=self._total_failures,
                total_successes=self._total_successes,
            )
