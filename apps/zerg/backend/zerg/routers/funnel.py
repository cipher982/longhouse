"""
Funnel tracking system for Swarmlet pre-launch.

Tracks the complete user journey from page load to conversion:
1. page_view - HTML was served
2. js_loaded - JavaScript executed (real browser)
3. human_detected - Mouse/scroll/keypress (not a bot)
4. cta_clicked - Clicked CTA button
5. signup_modal_opened - Opened auth modal
6. signup_submitted - Google OAuth initiated
7. signup_completed - User landed on dashboard
8. pricing_viewed - Pricing page loaded
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Optional

from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import JSONResponse

from zerg.config import get_settings

router = APIRouter(prefix="/funnel", tags=["funnel"])

settings = get_settings()

# Valid event types for client-side tracking
VALID_EVENTS = {
    # Page lifecycle
    "page_view",  # Page HTML loaded
    "js_loaded",  # JavaScript executed
    "human_detected",  # Mouse/scroll/key detected
    # CTA funnel
    "cta_clicked",  # Clicked Start Free or other CTA
    # Auth funnel
    "signup_modal_opened",  # Auth modal opened
    "signup_submitted",  # Google OAuth initiated
    "signup_completed",  # User landed on dashboard
    # Engagement
    "pricing_viewed",  # Pricing page loaded
    "scroll_25",  # Scrolled 25% of page
    "scroll_50",  # Scrolled 50%
    "scroll_75",  # Scrolled 75%
    "scroll_100",  # Scrolled to bottom
}

# Known bot user-agent patterns (lowercase)
KNOWN_BOT_PATTERNS = [
    "bot",
    "crawler",
    "spider",
    "scraper",
    "bingbot",
    "googlebot",
    "ahrefsbot",
    "semrushbot",
    "duckduckbot",
    "amazonbot",
    "seznambot",
    "mj12bot",
    "headlesschrome",
    "puppeteer",
    "phantomjs",
    "curl",
    "wget",
    "python-requests",
    "httpx",
    "palo alto",
    "nessus",
    "qualys",
]


def is_known_bot(user_agent: str) -> bool:
    """Check if user agent matches known bot patterns."""
    if not user_agent:
        return False
    ua_lower = user_agent.lower()
    return any(pattern in ua_lower for pattern in KNOWN_BOT_PATTERNS)


def get_funnel_db_path() -> Path:
    """Get path to funnel tracking database."""
    # Use settings.data_dir which handles environment-specific roots correctly
    if settings.testing:
        # In tests, use temp directory
        data_dir = Path("/tmp/zerg_test_data")
        data_dir.mkdir(parents=True, exist_ok=True)
    else:
        # Use shared data directory
        data_dir = settings.data_dir

    return data_dir / "funnel.db"


def get_funnel_db() -> sqlite3.Connection:
    """Get SQLite connection for funnel tracking."""
    db_path = get_funnel_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_funnel_tables() -> None:
    """Create funnel tracking tables."""
    conn = get_funnel_db()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS funnel_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            visitor_id TEXT NOT NULL,
            user_id TEXT,
            page_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            metadata TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_funnel_events_type_created
            ON funnel_events(event_type, created_at);
        CREATE INDEX IF NOT EXISTS idx_funnel_events_visitor
            ON funnel_events(visitor_id);
        CREATE INDEX IF NOT EXISTS idx_funnel_events_created
            ON funnel_events(created_at);
    """)
    conn.commit()
    conn.close()


# Initialize tables on module import
try:
    init_funnel_tables()
except Exception as e:
    print(f"Warning: Could not initialize funnel tables: {e}")


@dataclass
class FunnelEvent:
    """A single funnel event."""

    event_type: str
    visitor_id: str
    user_id: Optional[str]
    page_path: str
    timestamp: datetime
    metadata: dict[str, Any]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def record_event(
    event_type: str,
    visitor_id: str,
    page_path: str,
    user_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> bool:
    """Record a funnel event."""
    if event_type not in VALID_EVENTS:
        return False

    conn = get_funnel_db()
    cur = conn.cursor()
    now = _utcnow()
    now_iso = _iso(now)

    # Insert event
    cur.execute(
        """
        INSERT INTO funnel_events (event_type, visitor_id, user_id, page_path, created_at, metadata)
        VALUES (?, ?, ?, ?, ?, ?)
    """,
        (
            event_type,
            visitor_id,
            user_id,
            page_path,
            now_iso,
            json.dumps(metadata or {}),
        ),
    )

    conn.commit()
    conn.close()
    return True


def get_funnel_stats(
    since: datetime,
    until: Optional[datetime] = None,
) -> dict[str, Any]:
    """Get funnel statistics for a time period."""
    conn = get_funnel_db()
    cur = conn.cursor()

    since_iso = _iso(since)
    if until is None:
        until = _utcnow() + timedelta(seconds=1)
    until_iso = _iso(until)

    # Get event counts
    query = """
        SELECT event_type, COUNT(*) as count, COUNT(DISTINCT visitor_id) as unique_visitors
        FROM funnel_events
        WHERE created_at >= ? AND created_at < ?
        GROUP BY event_type
    """

    rows = cur.execute(query, (since_iso, until_iso)).fetchall()

    events = {row["event_type"]: {"count": row["count"], "unique": row["unique_visitors"]} for row in rows}

    # Calculate key funnel metrics
    page_views = events.get("page_view", {}).get("unique", 0)
    js_loaded = events.get("js_loaded", {}).get("unique", 0)
    humans = events.get("human_detected", {}).get("unique", 0)
    cta_clicks = events.get("cta_clicked", {}).get("unique", 0)
    modal_opens = events.get("signup_modal_opened", {}).get("unique", 0)
    signups_submitted = events.get("signup_submitted", {}).get("unique", 0)
    signups_completed = events.get("signup_completed", {}).get("unique", 0)
    pricing_viewed = events.get("pricing_viewed", {}).get("unique", 0)

    conn.close()

    return {
        "period": {"start": since_iso, "end": until_iso},
        "events": events,
        "funnel": {
            "page_view": page_views,
            "js_loaded": js_loaded,
            "human_detected": humans,
            "cta_clicked": cta_clicks,
            "signup_modal_opened": modal_opens,
            "signup_submitted": signups_submitted,
            "signup_completed": signups_completed,
            "pricing_viewed": pricing_viewed,
        },
        "conversion_rates": {
            "page_to_js": _safe_pct(js_loaded, page_views),
            "js_to_human": _safe_pct(humans, js_loaded),
            "human_to_cta": _safe_pct(cta_clicks, humans),
            "cta_to_modal": _safe_pct(modal_opens, cta_clicks),
            "modal_to_submit": _safe_pct(signups_submitted, modal_opens),
            "submit_to_complete": _safe_pct(signups_completed, signups_submitted),
            "overall_conversion": _safe_pct(signups_completed, page_views),
        },
    }


def _safe_pct(numerator: int, denominator: int) -> float:
    """Calculate percentage safely."""
    if denominator == 0:
        return 0.0
    return round((numerator / denominator) * 100, 1)


def stitch_visitor_to_user(visitor_id: str, user_id: str) -> int:
    """Backfill user_id on all funnel_events for a visitor.

    Call this when a user logs in or signs up to link their anonymous browsing
    history to their user account.

    Args:
        visitor_id: The visitor's cookie ID
        user_id: The authenticated user's ID

    Returns:
        Number of events updated
    """
    if not visitor_id or not user_id:
        return 0

    conn = get_funnel_db()
    cur = conn.cursor()

    # Update funnel_events
    cur.execute(
        """
        UPDATE funnel_events
        SET user_id = ?
        WHERE visitor_id = ? AND user_id IS NULL
    """,
        (user_id, visitor_id),
    )
    events_updated = cur.rowcount

    conn.commit()
    conn.close()
    return events_updated


# ==================== API ENDPOINTS ====================


def _is_valid_origin(request: Request) -> bool:
    """Check if request is from our domain (basic spoofing protection)."""
    # In dev/test, allow all origins
    if settings.auth_disabled or settings.testing:
        return True

    from urllib.parse import urlparse

    # Exact matches for production
    allowed_hosts = {
        "swarmlet.com",
        "www.swarmlet.com",
        "swarmlet.ai",
        "www.swarmlet.ai",
        "localhost",
        "127.0.0.1",
    }

    def _host_allowed(hostname: str | None) -> bool:
        if not hostname:
            return False
        if hostname in allowed_hosts:
            return True
        # Allow subdomains
        if hostname.endswith(".swarmlet.com") or hostname.endswith(".swarmlet.ai"):
            return True
        return False

    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")

    if origin:
        parsed = urlparse(origin)
        if _host_allowed(parsed.hostname):
            return True

    if referer:
        parsed = urlparse(referer)
        if _host_allowed(parsed.hostname):
            return True

    return False


@router.post("/batch")
async def track_batch(request: Request):
    """Track multiple events in one request (reduces network calls)."""
    if not _is_valid_origin(request):
        return JSONResponse({"ok": False}, status_code=403)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    events = data.get("events", [])
    visitor_id = data.get("visitor_id")

    if not visitor_id or not events:
        return JSONResponse({"ok": False, "error": "Missing visitor_id or events"}, status_code=400)

    # Get user_id from request state if authenticated
    user_id = getattr(request.state, "user_id", None)

    recorded = 0
    for event in events[:50]:  # Limit to 50 events per batch
        if record_event(
            event_type=event.get("event", ""),
            visitor_id=visitor_id,
            page_path=event.get("page", "/"),
            user_id=user_id,
            metadata=event.get("metadata", {}),
        ):
            recorded += 1

    return JSONResponse({"ok": True, "recorded": recorded})


@router.get("/stats")
async def get_stats(request: Request, hours: int = 24):
    """Get funnel statistics.

    Args:
        hours: Time period in hours (default 24)
    """
    # In production with auth, require authentication
    if not settings.auth_disabled and not settings.testing:
        user_id = getattr(request.state, "user_id", None)
        if not user_id:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

    since = _utcnow() - timedelta(hours=hours)
    stats = get_funnel_stats(since)
    return JSONResponse(stats)


@router.post("/stitch-visitor")
async def stitch_visitor(request: Request):
    """Link anonymous visitor to authenticated user."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    visitor_id = data.get("visitor_id")
    user_id = data.get("user_id")

    if not visitor_id or not user_id:
        return JSONResponse({"ok": False, "error": "Missing visitor_id or user_id"}, status_code=400)

    updated = stitch_visitor_to_user(visitor_id, user_id)
    return JSONResponse({"ok": True, "events_updated": updated})


@router.get("/attribution")
async def get_attribution(request: Request, hours: int = 72):
    """Get attribution breakdown by UTM source and campaign.

    Args:
        hours: Time period in hours (default 72 = 3 days)
    """
    # In production with auth, require authentication
    if not settings.auth_disabled and not settings.testing:
        user_id = getattr(request.state, "user_id", None)
        if not user_id:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

    conn = get_funnel_db()
    cur = conn.cursor()

    since = _utcnow() - timedelta(hours=hours)
    since_iso = _iso(since)

    # Query attribution data from metadata
    query = """
        SELECT
            json_extract(metadata, '$.first_touch.utm_source') AS utm_source,
            json_extract(metadata, '$.first_touch.utm_campaign') AS utm_campaign,
            COUNT(DISTINCT visitor_id) AS visitors,
            SUM(CASE WHEN event_type = 'signup_completed' THEN 1 ELSE 0 END) AS signups
        FROM funnel_events
        WHERE created_at >= ?
          AND json_extract(metadata, '$.first_touch.utm_source') IS NOT NULL
        GROUP BY 1, 2
        ORDER BY signups DESC, visitors DESC
    """

    rows = cur.execute(query, (since_iso,)).fetchall()

    attribution_data = [
        {
            "utm_source": row["utm_source"],
            "utm_campaign": row["utm_campaign"],
            "visitors": row["visitors"],
            "signups": row["signups"],
            "conversion_rate": _safe_pct(row["signups"], row["visitors"]),
        }
        for row in rows
    ]

    conn.close()

    return JSONResponse(
        {
            "period": {"start": since_iso, "end": _iso(_utcnow()), "hours": hours},
            "attribution": attribution_data,
            "total_visitors": sum(row["visitors"] for row in attribution_data),
            "total_signups": sum(row["signups"] for row in attribution_data),
        }
    )
