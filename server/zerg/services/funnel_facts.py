"""Content-free usage facts a consenting hosted tester's tenant keeps about itself.

Phase 0 of the first-users plan needs to know, for ~10 known testers, whether
each reached first backfill, first phone view and a day-7 return. This is the
tenant's half: a handful of counters and dates, kept in a tiny side file next to
the live catalog and read by the operating control plane through
`GET /api/internal/funnel` (routers/internal_funnel.py).

Ground rules, all enforced here:

* Off by default. `LONGHOUSE_FUNNEL_FACTS=1` is set on a tenant only by the
  control plane, at the moment an invited tester agrees to it. A self-hosted
  Runtime Host never sets it, so it records nothing and there is no route that
  answers.
* Pull only. Nothing is sent anywhere; the control plane asks, with the
  tenant's own derived secret.
* Content-free. The vocabulary is fixed below: a milestone name with its first
  and last time, and a (day, surface) pair. No query text, path, session id,
  user id, prompt or transcript ever reaches this module: `observe_request`
  looks at the method, the route, the response status, whether the caller was a
  signed-in person, and which kind of client it was.
* Never in the way. Every failure is swallowed; a request never waits on or
  fails because of this file. Writes are rare (once per milestone per hour, once
  per surface per day) and run off the event loop.

The catalog-derived facts (machines, sessions per provider) are read separately,
through one catalogd read RPC, and merged into the same document.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import threading
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from starlette.types import Scope

logger = logging.getLogger(__name__)

SCHEMA = "longhouse.tenant-funnel.v1"

# The complete vocabulary. Anything else is refused at the door of `note`.
MILESTONES = ("search", "steer", "phone_view", "web_view")
SURFACES = ("web", "ios")

# The iOS app's default User-Agent is `Longhouse/<build> CFNetwork/<v> Darwin/<v>`
# (PRODUCT_NAME=Longhouse); two requests set `Longhouse-iOS` explicitly. The
# widget (`LonghouseWidget/...`) is not a person looking at the app.
_IOS_USER_AGENT = re.compile(r"^(Longhouse-iOS\b|Longhouse/[^\s]+ CFNetwork/)")

# Routes whose success means "a person searched" / "a person sent an instruction".
# Wire paths, as the outermost middleware sees them (the API is mounted at /api).
_SEARCH_PATHS = frozenset({"/api/timeline/sessions", "/api/timeline/sessions/semantic", "/api/timeline/recall"})
_STEER_PATH = re.compile(r"^/api/sessions/[^/]+/(input|inputs-multipart|send-live)$")

# One write per milestone per hour keeps the file small and the writer idle.
_MILESTONE_BUCKET = "%Y-%m-%dT%H"

_SCHEMA_SQL = (
    "CREATE TABLE IF NOT EXISTS milestones ( name TEXT PRIMARY KEY, first_at TEXT NOT NULL, last_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS active_days (day TEXT NOT NULL, surface TEXT NOT NULL, PRIMARY KEY (day, surface))",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class FunnelFactsStore:
    """The side file. One short connection per write; writes are rare."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._seen: set[tuple[str, str]] = set()
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=1.0)
        if not self._ready:
            for statement in _SCHEMA_SQL:
                connection.execute(statement)
            connection.commit()
            self._ready = True
        return connection

    @staticmethod
    def _keys(surface: str, milestones: list[str], moment: datetime) -> list[tuple[str, str]]:
        return [(f"active:{surface}", moment.strftime("%Y-%m-%d"))] + [(name, moment.strftime(_MILESTONE_BUCKET)) for name in milestones]

    def pending(self, surface: str, milestones: list[str], *, at: datetime | None = None) -> bool:
        """Whether recording this observation would write anything new."""
        with self._lock:
            return any(key not in self._seen for key in self._keys(surface, milestones, at or _now()))

    def _mark(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._seen.add(key)

    def note_milestone(self, name: str, *, at: datetime | None = None) -> bool:
        """Record that `name` just happened. Returns whether a write was due."""
        if name not in MILESTONES:
            raise ValueError(f"unknown milestone {name!r}")
        moment = at or _now()
        key = (name, moment.strftime(_MILESTONE_BUCKET))
        if key in self._seen:
            return False
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO milestones (name, first_at, last_at) VALUES (?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET last_at = excluded.last_at",
                (name, _stamp(moment), _stamp(moment)),
            )
        self._mark(key)  # only after the write landed: a failure is retried on the next request
        return True

    def note_active(self, surface: str, *, at: datetime | None = None) -> bool:
        """Record that a person used `surface` today (UTC). Once per day per surface."""
        if surface not in SURFACES:
            raise ValueError(f"unknown surface {surface!r}")
        moment = at or _now()
        key = (f"active:{surface}", moment.strftime("%Y-%m-%d"))
        if key in self._seen:
            return False
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO active_days (day, surface) VALUES (?, ?)", (key[1], surface))
        self._mark(key)
        return True

    def snapshot(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"milestones": {}, "active_days": {surface: [] for surface in SURFACES}}
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=1.0)
        try:
            milestones = {
                name: {"first_at": first, "last_at": last}
                for name, first, last in connection.execute("SELECT name, first_at, last_at FROM milestones")
            }
            days: dict[str, list[str]] = {surface: [] for surface in SURFACES}
            for day, surface in connection.execute("SELECT day, surface FROM active_days ORDER BY day"):
                days.setdefault(surface, []).append(day)
        finally:
            connection.close()
        return {"milestones": milestones, "active_days": days}


_store: FunnelFactsStore | None = None
_store_guard = threading.Lock()


def funnel_facts_path() -> Path | None:
    """The side file lives beside the live catalog; None when that is not a file."""
    from zerg.config import get_settings
    from zerg.config import sqlite_file_path

    live = sqlite_file_path(get_settings().live_database_url)
    return live.parent / "funnel-facts.sqlite3" if live is not None else None


def get_store() -> FunnelFactsStore | None:
    global _store
    with _store_guard:
        if _store is None:
            path = funnel_facts_path()
            if path is None:
                return None
            _store = FunnelFactsStore(path)
        return _store


def reset_store_for_tests() -> None:
    global _store
    with _store_guard:
        _store = None


def enabled() -> bool:
    from zerg.config import get_settings

    return bool(get_settings().funnel_facts_enabled)


def _headers(scope: Scope) -> dict[str, str]:
    return {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", ())}


def classify(scope: Scope, status_code: int) -> tuple[str | None, list[str]]:
    """Decide what one finished request means: (surface, milestones).

    Only a signed-in person's successful request counts. A machine agent shipping
    in the background (`device:` principal), an agent (`session:`), a probe and a
    rejected request are all ignored, so background traffic can never look like a
    tester coming back.
    """
    if scope.get("type") != "http" or not 200 <= status_code < 300:
        return None, []
    state = scope.get("state") or {}
    if not str(state.get("principal") or "").startswith("user:"):
        return None, []

    headers = _headers(scope)
    authorization = headers.get("authorization", "")
    user_agent = headers.get("user-agent", "")
    if authorization.lower().startswith("bearer "):
        surface = "ios" if _IOS_USER_AGENT.match(user_agent) else None
    else:
        surface = "web" if headers.get("cookie") and "mozilla" in user_agent.lower() else None
    if surface is None:
        return None, []

    milestones = ["phone_view" if surface == "ios" else "web_view"]
    path, method = str(scope.get("path", "")), scope.get("method", "")
    # Token refresh and long-lived streams are what an open-but-unused tab or a
    # backgrounded app does on its own; they are not a person coming back.
    if path.startswith("/api/auth/") or path.endswith("/stream"):
        return None, []
    if method == "GET" and path in _SEARCH_PATHS:
        query = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("query", [""])[0]
        if query.strip():
            milestones.append("search")
    elif method == "POST" and _STEER_PATH.match(path):
        milestones.append("steer")
    return surface, milestones


def _record(store: FunnelFactsStore, surface: str, milestones: list[str]) -> None:
    try:
        store.note_active(surface)
        for name in milestones:
            store.note_milestone(name)
    except Exception:  # noqa: BLE001 - facts must never affect the product
        logger.warning("funnel facts write failed", exc_info=True)


def observe_request(scope: Scope, status_code: int) -> None:
    """Called once per finished request by the access-log middleware."""
    try:
        if not enabled():
            return
        surface, milestones = classify(scope, status_code)
        if surface is None:
            return
        store = get_store()
        if store is None:
            return
        # Cheap pre-check on the event loop: only hand a write to the executor
        # when one is actually due.
        if store.pending(surface, milestones):
            asyncio.get_running_loop().run_in_executor(None, _record, store, surface, milestones)
    except Exception:  # noqa: BLE001
        logger.warning("funnel facts observe failed", exc_info=True)


def build_document(catalog_facts: dict[str, Any], side_facts: dict[str, Any]) -> dict[str, Any]:
    """The one document the control plane reads: catalog facts plus the side file."""
    devices = catalog_facts.get("devices") or {}
    return {
        "schema": SCHEMA,
        "generated_at": _stamp(_now()),
        "machines": {
            "count": int(devices.get("count") or 0),
            "first_connected_at": devices.get("first_created_at"),
            "last_seen_at": devices.get("last_used_at"),
        },
        "providers": {
            row["provider"]: {"sessions": int(row["sessions"]), "first_shipped_at": row.get("first_shipped_at")}
            for row in catalog_facts.get("providers") or []
        },
        "milestones": side_facts.get("milestones") or {},
        "active_days": side_facts.get("active_days") or {},
    }
