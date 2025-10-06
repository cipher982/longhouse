"""Generic *async* retry decorator with exponential back-off + jitter.

This helper isolates retry logic in one place so call-sites stay concise
(``await gmail_api.list_history(...)``) while the policy (max attempts,
back-off, metrics, logging) can be evolved centrally.

Usage
-----

```python
from zerg.utils.retry import async_retry


@async_retry(provider="gmail")
async def list_history(access_token: str, start_hid: int) -> list[dict]:
    ...
```

Parameters can be tuned per-function:

```python
@async_retry(max_attempts=4, base_delay=1.0, max_delay=16.0, jitter=0.3)
```

Why implement in-house instead of using *tenacity*?
• We need **no** sync wrappers – only ``async def`` support.
• Want *zero* third-party deps in the critical path (tenacity pulls extras).
• The entire implementation is <60 LOC and tailored to our metrics + log
  layer (``structlog``).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from typing import Awaitable
from typing import Callable

# ---------------------------------------------------------------------------
# Typing helpers – runtime is Python ≥3.12 so ``ParamSpec`` exists in stdlib.
# ---------------------------------------------------------------------------
# Python <3.10 compatibility – fallback to `typing_extensions`.
try:
    from typing import ParamSpec  # type: ignore
except ImportError:  # pragma: no cover – old interpreter
    from typing_extensions import ParamSpec  # type: ignore
from typing import TypeVar

# Centralised settings access
from zerg.config import get_settings
from zerg.metrics import external_api_retry_total
from zerg.metrics import gmail_api_error_total

log = logging.getLogger(__name__)

_T = TypeVar("_T")
_P = ParamSpec("_P")


def _default_retriable(exc: Exception) -> bool:  # noqa: D401 – small helper
    """Retry **everything** by default (caller can override)."""

    return True


def async_retry(
    *,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: float = 0.25,
    retriable: Callable[[Exception], bool] | None = None,
    provider: str | None = None,  # Metric label only – optional
) -> Callable[[Callable[_P, Awaitable[_T]]], Callable[_P, Awaitable[_T]]]:
    """Decorate an *async* function so it is executed with retry semantics.

    Parameters
    ----------
    max_attempts:
        Inclusive – the *first* try counts. ``max_attempts=1`` disables retry.
    base_delay:
        Initial sleep in seconds (doubles on every retry).
    max_delay:
        Upper bound for back-off sleep.
    jitter:
        0-1.0 – percentage of random noise added/subtracted from delay.
    retriable:
        Callback deciding if *exc* is worth another attempt. Defaults to
        retrying **all** exceptions.
    provider:
        Optional string used for metrics label (e.g. "gmail", "slack").
    """

    # Shrink retry duration dramatically when running inside the *unit-test*
    # harness so slow external-path tests do not dominate runtime.
    if get_settings().testing:
        max_attempts = min(max_attempts, 2)
        base_delay = min(base_delay, 0.01)
        max_delay = min(max_delay, 0.05)

    retriable = retriable or _default_retriable

    def decorator(fn: Callable[_P, Awaitable[_T]]) -> Callable[_P, Awaitable[_T]]:
        @functools.wraps(fn)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _T:  # type: ignore[misc]
            attempt = 1
            delay = base_delay

            while True:
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:
                    if attempt >= max_attempts or not retriable(exc):
                        log.warning(
                            "retry-exhausted",
                            provider=provider or fn.__module__,
                            function=fn.__name__,
                            attempts=attempt,
                            error=str(exc),
                        )
                        # Increment *error* counter so alerts fire on final failure
                        gmail_api_error_total.inc()  # Generic until we add per-provider counter
                        raise

                    # ------------------------------------------------------------------
                    # Retry path ---------------------------------------------------------
                    # ------------------------------------------------------------------

                    sleep_for = delay * (1 + random.uniform(-jitter, jitter))
                    log.debug(
                        "retry",
                        provider=provider or fn.__module__,
                        function=fn.__name__,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        sleep=sleep_for,
                    )

                    # Increment generic retry metric with labels
                    try:
                        external_api_retry_total.labels(provider or fn.__module__, fn.__name__).inc()
                    except Exception:  # pragma: no cover – metrics disabled
                        pass

                    await asyncio.sleep(sleep_for)

                    attempt += 1
                    delay = min(delay * 2, max_delay)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
#   Helpers for common HTTP status handling
# ---------------------------------------------------------------------------


def is_retryable_http_exc(exc: Exception) -> bool:  # noqa: D401 – helper
    """Return *True* if exception indicates a transient HTTP failure.

    Expects the caller to wrap provider-specific SDK errors and expose
    ``status_code`` attribute (integer) akin to ``httpx.HTTPStatusError``.
    """

    status = getattr(exc, "status_code", None)
    if status is None:
        return True  # Network / parsing error → retry

    return status in {429, 500, 502, 503, 504}


__all__ = [
    "async_retry",
    "is_retryable_http_exc",
]
