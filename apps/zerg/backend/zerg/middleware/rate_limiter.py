"""Simple in-memory rate limiter for preventing accidental API spam."""

import time
from collections import defaultdict
from typing import Dict
from typing import Optional

from fastapi import HTTPException
from fastapi import Request


class SimpleRateLimiter:
    """In-memory rate limiter with sliding window."""

    def __init__(self):
        # user_id -> list of request timestamps
        self._requests: Dict[int, list] = defaultdict(list)

    def is_allowed(self, user_id: int, limit: int, window_seconds: int) -> bool:
        """Check if request is allowed under rate limit."""
        now = time.time()
        user_requests = self._requests[user_id]

        # Remove old requests outside the window
        cutoff = now - window_seconds
        user_requests[:] = [ts for ts in user_requests if ts > cutoff]

        # Check if under limit
        if len(user_requests) >= limit:
            return False

        # Add current request
        user_requests.append(now)
        return True

    def get_retry_after(self, user_id: int, window_seconds: int) -> Optional[int]:
        """Get seconds until next request allowed (for 429 header)."""
        user_requests = self._requests[user_id]
        if not user_requests:
            return None

        oldest_in_window = user_requests[0]
        retry_after = int(oldest_in_window + window_seconds - time.time())
        return max(1, retry_after)


# Global rate limiter instance
rate_limiter = SimpleRateLimiter()


def check_workflow_creation_rate_limit(request: Request, user_id: int):
    """Rate limit workflow creation: 100 per minute per user."""
    if not rate_limiter.is_allowed(user_id, limit=100, window_seconds=60):
        retry_after = rate_limiter.get_retry_after(user_id, window_seconds=60)
        raise HTTPException(
            status_code=429,
            detail="Too many workflow creation requests. Limit: 100 per minute.",
            headers={"Retry-After": str(retry_after)} if retry_after else {},
        )
