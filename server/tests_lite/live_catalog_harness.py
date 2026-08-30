"""A real live catalog, for any test that needs the production code path.

``tests_lite`` used to run entirely on the legacy branch. ``TESTING=1`` made
``live_catalog_enabled()`` return false, so every route reached for SQLAlchemy
while production reached for catalogd, and no test in the suite ever executed
the branch a Runtime Host actually takes. That early return is gone. What is
left is the other half of the problem: a route that reaches for catalogd with
no daemon behind it answers 503, and a 503 is not the production behaviour
either.

So this module provisions the daemon. It stands up a real ``CatalogDaemon`` and
a real ``SearchDaemon`` over real Unix sockets, shapes the environment the way a
self-hosted Runtime Host shapes it, and hands back a small facade for seeding
owners, machine tokens and transcripts. Nothing is mocked; every authorization
decision, RPC and SQL statement underneath is the one production runs.

Two ways in, one implementation::

    def test_a_route(live_catalog, live_catalog_client):
        owner = live_catalog.create_user("owner@example.test")
        token = live_catalog.create_device_token(owner_id=owner, device_id="cinder")
        response = live_catalog_client.get("/agents/sessions", headers={"X-Agents-Token": token})

    def test_a_route_from_the_body():
        with provision_live_catalog() as live:
            with live.http_client() as client:
                ...

Two seams are set rather than mocked, and only because Python binds them at
import time and the test process imported them under the test environment:

* the settings copies ``zerg.database`` and the auth dependency cached at
  import are refreshed, so ``live_catalog_enabled()`` and the auth strategy
  selector answer the way they answer on a Runtime Host;
* ``catalogd_supervisor._supervisor`` and its searchd twin are pointed at the
  daemons started here, since nothing in a test runs the real supervisors.

This is a helper module and deliberately not a ``conftest``: it applies to the
tests that import it and to no others, and every test still owns its own
database, its own object root and its own daemons.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from typing import Iterator
from uuid import UUID
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

# Import-time defaults only. They exist so importing ``zerg.main`` cannot fail
# when this module is imported first; every provisioned catalog sets the
# environment it actually depends on, so nothing below depends on which module
# imported first.
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

import zerg.database as database_module  # noqa: E402
from zerg.auth import managed_session_tokens as managed_tokens  # noqa: E402
from zerg.auth import session_tokens  # noqa: E402
from zerg.catalogd.client import CatalogClient  # noqa: E402
from zerg.catalogd.server import CatalogDaemon  # noqa: E402
from zerg.config import get_settings_unchecked  # noqa: E402
from zerg.dependencies import auth as auth_deps  # noqa: E402

# Importing ``api_app`` is what guarantees every router module is loaded before
# the db dependencies FastAPI captured are collected below.
from zerg.main import api_app  # noqa: E402
from zerg.searchd.server import SearchDaemon  # noqa: E402
from zerg.services import catalog_read_gateway  # noqa: E402
from zerg.services import catalogd_supervisor  # noqa: E402
from zerg.services import raw_object_workers  # noqa: E402
from zerg.services import render_object_workers  # noqa: E402
from zerg.services import searchd_supervisor  # noqa: E402
from zerg.services.search_v2_projector import SearchV2Projector  # noqa: E402
from zerg.storage_v2.contracts import EnvelopeIdentity  # noqa: E402
from zerg.storage_v2.contracts import envelope_id as compute_envelope_id  # noqa: E402
from zerg.storage_v2.contracts import hash_records  # noqa: E402
from zerg.storage_v2.raw_objects import RawObjectSpec  # noqa: E402
from zerg.storage_v2.raw_objects import RawRecord  # noqa: E402
from zerg.storage_v2.raw_objects import read_raw_object  # noqa: E402
from zerg.storage_v2.raw_objects import seal_raw_object  # noqa: E402
from zerg.storage_v2.render_objects import RenderObjectSpec  # noqa: E402
from zerg.storage_v2.render_objects import RenderRecord  # noqa: E402
from zerg.storage_v2.render_objects import read_render_object  # noqa: E402
from zerg.storage_v2.render_objects import seal_render_object  # noqa: E402

__all__ = [
    "DEFAULT_INSTANCE_ID",
    "DEFAULT_INTERNAL_SECRET",
    "DEFAULT_JWT_SECRET",
    "LiveCatalog",
    "PARSER_REVISION",
    "RPC_TIMEOUT_SECONDS",
    "SeededSession",
    "floored_rpc_timeout",
    "live_catalog",
    "live_catalog_client",
    "provision_live_catalog",
]

RPC_TIMEOUT_SECONDS = 15.0
# Every catalog here is a daemon started milliseconds ago, sharing one machine
# with the test process and, across a full suite run, with several thousand
# other tests. The gateway's production budget (0.35s per attempt, 0.75s total)
# is tuned for a long-lived daemon on its own host; under suite load it turns a
# healthy `session.read.v2` into a timeout, and the route reports that as
# "session not found". These tests assert behaviour, never latency, so the read
# gets room. Production budgets are untouched.
READ_BUDGET_SCALE = 20.0
# The tenant a Runtime Host writes under is derived from INSTANCE_ID, so direct
# catalog commits use the same one the HTTP ingest route computes.
DEFAULT_INSTANCE_ID = "instance-a"
# Long enough to satisfy the production configuration gate, which refuses to
# start with a weak or default signing key when auth is enabled.
DEFAULT_JWT_SECRET = "live-catalog-harness-jwt-secret-0123456789"
DEFAULT_INTERNAL_SECRET = "live-catalog-harness-internal-secret-0123456789"
PARSER_REVISION = "live-catalog-harness-v1"

# ``/tmp`` stays short after macOS resolves it, which keeps the Unix socket path
# under the 104-byte limit ``catalogd_paths()`` also works around.
_ROOT_PARENT = Path("/tmp")
_ROOT_PREFIX = "lh-live-catalog"


# ---------------------------------------------------------------------------
# Process-wide seams
# ---------------------------------------------------------------------------


class _DaemonLoop:
    """A dedicated event loop thread for the daemons.

    catalogd is a separate *process* in production, so a synchronous
    ``call_catalogd_sync`` inside a request never blocks the daemon that has to
    answer it. In one process the two must at least not share an event loop, or
    the first blocking auth call deadlocks against its own answer.
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="live-catalog-daemons", daemon=True)
        self._thread.start()

    def run(self, coro, *, timeout: float = 60.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10.0)
        self.loop.close()


class _PerCallSupervisor:
    """Daemon supervisor stand-in that hands out a fresh client per call.

    ``CatalogClient`` binds its admission semaphore to the first event loop that
    uses it, and a test drives one socket from two loops (the daemons' and the
    TestClient's portal). One client per call keeps that honest without changing
    a byte of the RPC path.
    """

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = socket_path

    @property
    def client(self) -> CatalogClient:
        return CatalogClient(self._socket_path, default_timeout_seconds=RPC_TIMEOUT_SECONDS)

    @property
    def projector_client(self) -> CatalogClient:
        return self.client


class _RenderReader:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def read(
        self,
        object_path: str,
        object_hash: str,
        *,
        lane: str,
        queue_timeout_seconds: float | None = None,
    ):
        return read_render_object(self.root, object_path, expected_object_hash=object_hash)


class _RawReader:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def read(
        self,
        object_path: str,
        object_hash: str,
        tenant_id: str,
        *,
        queue_timeout_seconds: float | None = None,
    ):
        return read_raw_object(self.root, object_path, expected_object_hash=object_hash)


def _no_db() -> None:
    """The value a live Runtime Host binds every catalog db dependency to."""

    return None


_MISSING = object()
_DB_DEPENDENCY_SUFFIX = "db_dependency"
_DB_DEPENDENCY_EXTRA_NAMES = frozenset({"_auth_compat_db"})


def _import_time_db_dependencies() -> set[Any]:
    """Return the db dependencies FastAPI captured in the route signatures.

    Every router picks its catalog db dependency exactly once, at import, from
    ``live_catalog_enabled()`` / ``settings.testing``. A live Runtime Host binds
    each of them to a generator that yields ``None``; a process that imported
    them under ``TESTING=1`` binds them to ``get_db``. Rebinding the module
    attribute afterwards changes nothing, because FastAPI already captured the
    callable, so the value is replaced through ``dependency_overrides``.

    Several distinct roles collapse onto the single ``get_db`` object in a test
    process, so overriding them overrides every ``Depends(get_db)`` in the app.
    That is the seam the routers document -- "keep ``get_db`` as the exact
    callable in legacy/test mode so dependency overrides still bind" -- and the
    resulting value is what a live Runtime Host hands those routes: no
    SQLAlchemy session at all. A test that needs one route to keep a real
    archive session can pass its own entry through ``extra_overrides``.
    """

    modules = [module for name, module in list(sys.modules.items()) if name.startswith("zerg.routers.") and module is not None]
    modules.append(auth_deps)
    dependencies: set[Any] = set()
    for module in modules:
        for attribute in list(vars(module)):
            if not (attribute.endswith(_DB_DEPENDENCY_SUFFIX) or attribute in _DB_DEPENDENCY_EXTRA_NAMES):
                continue
            candidate = getattr(module, attribute, None)
            if callable(candidate):
                dependencies.add(candidate)
    return dependencies


def _live_root() -> Path:
    root = _ROOT_PARENT / f"{_ROOT_PREFIX}-{uuid4().hex[:10]}"
    root.mkdir(mode=0o700)
    return root


def _reset_storage_worker_pools() -> None:
    raw_object_workers._pool = None
    render_object_workers._pool = None


def floored_rpc_timeout(timeout_seconds: float | None) -> float | None:
    """The floor itself, separated so it can be asserted rather than inferred.

    ``None`` keeps the client's own default. Anything at or above the floor is
    left alone. Only a tighter explicit budget is raised.
    """

    if timeout_seconds is None or timeout_seconds >= RPC_TIMEOUT_SECONDS:
        return timeout_seconds
    return RPC_TIMEOUT_SECONDS


def _floor_client_rpc_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise any explicit per-call RPC timeout to the harness floor.

    Production call sites pass tight literals -- ``timeout_seconds=1.0`` on
    ``session.input.recent.list.v2``, and 25 others under a second -- which are
    right for a long-lived daemon on its own host. Here the daemon started
    milliseconds ago and shares a machine with several thousand other tests, so
    a healthy call exceeds one second on a loaded CI runner and the route
    reports the timeout as "unavailable".

    ``_widen_catalog_read_budgets`` cannot reach these: it scales the module
    globals in ``catalog_read_gateway``, and these calls go straight to
    ``CatalogClient.call`` with the number written at the call site.

    This is a budget, never a behaviour: the RPC, its retry rule and its error
    mapping are untouched. Production numbers are untouched too -- the floor
    exists only inside a provisioned catalog.
    """

    original_call = CatalogClient.call

    async def _floored(self, method, params=None, *, timeout_seconds=None):
        return await original_call(
            self, method, params, timeout_seconds=floored_rpc_timeout(timeout_seconds)
        )

    monkeypatch.setattr(CatalogClient, "call", _floored)


def _widen_catalog_read_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scale the bounded-read budgets for the life of one provisioned catalog.

    ``_call`` reads the deadline and per-attempt budget from module globals on
    every call, so scaling them here changes what this process allows without
    editing the numbers production ships.
    """

    monkeypatch.setattr(
        catalog_read_gateway,
        "_DEFAULT_DEADLINE_SECONDS",
        catalog_read_gateway._DEFAULT_DEADLINE_SECONDS * READ_BUDGET_SCALE,
    )
    monkeypatch.setattr(
        catalog_read_gateway,
        "_DEFAULT_ATTEMPT_SECONDS",
        catalog_read_gateway._DEFAULT_ATTEMPT_SECONDS * READ_BUDGET_SCALE,
    )
    monkeypatch.setattr(
        catalog_read_gateway,
        "_READ_BUDGETS",
        {
            method: (deadline * READ_BUDGET_SCALE, attempt * READ_BUDGET_SCALE)
            for method, (deadline, attempt) in catalog_read_gateway._READ_BUDGETS.items()
        },
    )


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_dir():
            child.rmdir()
        else:
            child.unlink(missing_ok=True)
    path.rmdir()


# ---------------------------------------------------------------------------
# The facade
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeededSession:
    session_id: UUID
    owner_id: int
    envelope_id: str
    raw_path: str
    render_path: str


class LiveCatalog:
    """A real live catalog: catalogd, searchd, and objects on disk."""

    def __init__(self, root: Path, loop: _DaemonLoop, catalog_socket: Path, search_socket: Path, tenant: str) -> None:
        self.root = root
        self.object_root = root / "objects-v2"
        self.loop = loop
        self.catalog_socket = catalog_socket
        self.search_socket = search_socket
        self.tenant = tenant
        self._catalog = CatalogClient(catalog_socket, default_timeout_seconds=RPC_TIMEOUT_SECONDS)
        self._search = CatalogClient(search_socket, default_timeout_seconds=RPC_TIMEOUT_SECONDS)

    # -- HTTP ------------------------------------------------------------

    @contextmanager
    def http_client(self, *, extra_overrides: dict[Any, Any] | None = None, **client_kwargs: Any) -> Iterator[TestClient]:
        """``api_app`` with its import-time db dependencies bound to production's.

        Any prior override is restored on exit, so a test may install its own
        before or after this one.
        """

        overrides: dict[Any, Any] = {dependency: _no_db for dependency in _import_time_db_dependencies()}
        overrides.update(extra_overrides or {})
        previous = {dependency: api_app.dependency_overrides.get(dependency, _MISSING) for dependency in overrides}
        api_app.dependency_overrides.update(overrides)
        try:
            with TestClient(api_app, **client_kwargs) as client:
                yield client
        finally:
            for dependency, prior in previous.items():
                if prior is _MISSING:
                    api_app.dependency_overrides.pop(dependency, None)
                else:
                    api_app.dependency_overrides[dependency] = prior

    # -- raw RPC ---------------------------------------------------------

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.loop.run(self._catalog.call(method, params or {}))

    def search_rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.loop.run(self._search.call(method, params or {}))

    def catalog_client(self) -> CatalogClient:
        """A fresh catalogd client, for code that holds one across calls."""

        return CatalogClient(self.catalog_socket, default_timeout_seconds=RPC_TIMEOUT_SECONDS)

    def search_client(self) -> CatalogClient:
        """A fresh searchd client, for code that holds one across calls."""

        return CatalogClient(self.search_socket, default_timeout_seconds=RPC_TIMEOUT_SECONDS)

    # -- identities ------------------------------------------------------

    def create_user(self, email: str, *, role: str = "USER") -> int:
        result = self.rpc(
            "auth.user.resolve_local.v2",
            {
                "email": email,
                "provider": "local",
                "provider_user_id": email,
                "role": role,
                "adopt_existing": False,
                "require_email_match": False,
                "max_users": None,
                "promote_role": True,
            },
        )
        return int(result["user"]["id"])

    def create_device_token(self, *, owner_id: int, device_id: str) -> str:
        token = f"zdt_{secrets.token_urlsafe(32)}"
        self.rpc(
            "auth.device.create.v2",
            {
                "owner_id": owner_id,
                "token_id": str(uuid4()),
                "device_id": device_id,
                "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            },
        )
        return token

    def browser_cookie(self, *, owner_id: int, email: str) -> str:
        """A ``longhouse_session`` cookie value for browser-authenticated routes."""

        return session_tokens._issue_access_token(owner_id, email)

    # -- transcripts -----------------------------------------------------

    def _session_facts(self, *, project: str, now: datetime) -> dict[str, Any]:
        return {
            "environment": "production",
            "project": project,
            "cwd": f"/workspace/{project}",
            "git_repo": "cipher982/longhouse",
            "git_branch": "main",
            "started_at": now.isoformat(),
            "last_activity_at": now.isoformat(),
            "ended_at": None,
            "origin_kind": "shadow",
            "hidden_from_default_timeline": False,
            "launch_actor": None,
            "launch_surface": None,
        }

    def _render_records(self, texts: tuple[str, ...], *, envelope: str, now: datetime, thread_id: str) -> list[dict[str, Any]]:
        return [
            {
                "event_id": f"event-{envelope[:12]}-{index}",
                "order_time_us": int(now.timestamp() * 1_000_000) + index,
                "source_position": index,
                "event_subordinal": 0,
                "role": "user",
                "content_text": text,
                "tool_name": None,
                "tool_input_json": None,
                "tool_output_text": None,
                "tool_call_id": None,
                "thread_id": thread_id,
                "branch_kind": "head",
                "raw_record_ordinal": index,
            }
            for index, text in enumerate(texts)
        ]

    def commit_session(
        self,
        *,
        owner_id: int,
        session_id: UUID | None = None,
        texts: tuple[str, ...] = ("hello from the transcript",),
        project: str = "longhouse",
        device_id: str = "cinder",
        now: datetime | None = None,
    ) -> SeededSession:
        """Seal a raw + render pair on disk and commit it to the live catalog."""

        session_id = session_id or uuid4()
        now = now or datetime.now(UTC).replace(microsecond=0)
        source_epoch = uuid4()
        generation_id = uuid4()
        thread_id = str(uuid4())
        opaque_source_id = f"machine-agent/{source_epoch}.jsonl"
        records = tuple(RawRecord(source_position=index, data=text.encode()) for index, text in enumerate(texts))
        sealed_raw = seal_raw_object(
            self.object_root,
            RawObjectSpec(
                tenant_id=self.tenant,
                machine_id=device_id,
                session_id=session_id,
                provider="codex",
                opaque_source_id=opaque_source_id,
                source_epoch=source_epoch,
                range_kind="record_ordinal",
                range_start=0,
                range_end=len(records),
                records=records,
            ),
        )
        render_records = self._render_records(texts, envelope=sealed_raw.envelope_id, now=now, thread_id=thread_id)
        sealed_render = seal_render_object(
            self.object_root,
            RenderObjectSpec(
                session_id=session_id,
                render_generation=generation_id,
                parser_revision=PARSER_REVISION,
                ordering_revision="semantic-order-v2",
                machine_id=device_id,
                provider="codex",
                opaque_source_id=opaque_source_id,
                source_epoch=source_epoch,
                source_envelope_id=sealed_raw.envelope_id,
                records=tuple(RenderRecord(**record) for record in render_records),
            ),
        )
        self.rpc(
            "storage.raw_object.commit.v2",
            {
                "protocol_version": 2,
                "tenant_id": self.tenant,
                "owner_id": str(owner_id),
                "session_id": str(session_id),
                "machine_id": device_id,
                "provider": "codex",
                "opaque_source_id": opaque_source_id,
                "source_epoch": str(source_epoch),
                "predecessor_source_epoch": None,
                "epoch_opened_at": now.isoformat(),
                "range_kind": "record_ordinal",
                "range_start": 0,
                "range_end": len(records),
                "record_hashes": list(sealed_raw.record_hashes),
                "envelope_id": sealed_raw.envelope_id,
                "object_hash": sealed_raw.object_hash,
                "payload_hash": sealed_raw.payload_hash,
                "compressed_hash": sealed_raw.compressed_hash,
                "object_path": sealed_raw.object_path,
                "uncompressed_size": sealed_raw.uncompressed_size,
                "compressed_size": sealed_raw.compressed_size,
                "provenance_kind": "native",
                "render_state": "ready",
                "media_refs": [],
                "projectors": ["search-v2"],
                "render_manifest": {
                    "generation_id": str(generation_id),
                    "parser_revision": PARSER_REVISION,
                    "ordering_revision": "semantic-order-v2",
                    "object_id": sealed_render.object_id,
                    "object_hash": sealed_render.object_hash,
                    "payload_hash": sealed_render.payload_hash,
                    "object_path": sealed_render.object_path,
                    "uncompressed_size": sealed_render.uncompressed_size,
                    "compressed_size": sealed_render.compressed_size,
                    "event_count": sealed_render.event_count,
                    "first_order_key": sealed_render.first_order_key,
                    "last_order_key": sealed_render.last_order_key,
                    "user_messages": sealed_render.user_messages,
                    "assistant_messages": sealed_render.assistant_messages,
                    "tool_calls": sealed_render.tool_calls,
                    "first_user_message_preview": sealed_render.first_user_message_preview,
                    "last_visible_text_preview": sealed_render.last_visible_text_preview,
                },
                "session_facts": self._session_facts(project=project, now=now),
                "sealed_at": now.isoformat(),
            },
        )
        return SeededSession(
            session_id=session_id,
            owner_id=owner_id,
            envelope_id=sealed_raw.envelope_id,
            raw_path=sealed_raw.object_path,
            render_path=sealed_render.object_path,
        )

    def envelope_body(
        self,
        *,
        session_id: UUID,
        device_id: str,
        texts: tuple[str, ...],
        project: str = "longhouse",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Build the protocol-v2 wire envelope the Machine Agent ships.

        The body deliberately carries no owner: owner identity comes from the
        token, and this is the payload an attacker controls end to end.
        """

        now = now or datetime.now(UTC).replace(microsecond=0)
        source_epoch = uuid4()
        opaque_source_id = f"machine-agent/{source_epoch}.jsonl"
        payloads = tuple(text.encode() for text in texts)
        identity = EnvelopeIdentity(
            tenant_id=self.tenant,
            machine_id=device_id,
            provider="codex",
            opaque_source_id=opaque_source_id,
            source_epoch=source_epoch,
            range_kind="record_ordinal",
            range_start=0,
            range_end=len(payloads),
            record_hashes=hash_records(payloads),
        )
        envelope = compute_envelope_id(identity)
        return {
            "protocol_version": 2,
            "tenant_id": self.tenant,
            "machine_id": device_id,
            "session_id": str(session_id),
            "provider": "codex",
            "opaque_source_id": opaque_source_id,
            "source_epoch": str(source_epoch),
            "predecessor_source_epoch": None,
            "epoch_opened_at": now.isoformat(),
            "range_kind": "record_ordinal",
            "range_start": 0,
            "range_end": len(payloads),
            "render": {
                "generation_id": str(uuid4()),
                "parser_revision": PARSER_REVISION,
                "ordering_revision": "semantic-order-v2",
                "records": self._render_records(texts, envelope=envelope, now=now, thread_id=str(uuid4())),
            },
            "media": [],
            "session": self._session_facts(project=project, now=now),
            "records": [
                {"source_position": index, "data_b64": base64.b64encode(data).decode("ascii")} for index, data in enumerate(payloads)
            ],
            "expected_envelope_id": envelope,
        }

    # -- search ----------------------------------------------------------

    def index_search(self, *, now: datetime | None = None) -> int:
        """Drain the real search-v2 projector into the real search index."""

        observed_at = now or datetime.now(UTC)
        projector = SearchV2Projector(
            catalog=self._catalog,
            search=self._search,
            render_workers=_RenderReader(self.object_root),
            raw_workers=_RawReader(self.object_root),
            worker_id="live-catalog-harness",
        )
        total = 0
        for step in range(25):
            claimed = self.loop.run(projector.run_once(now=observed_at + timedelta(seconds=step)))
            if claimed == 0:
                break
            total += claimed
        return total

    def search(self, *, owner_id: int, query: str, limit: int = 10) -> list[dict[str, Any]]:
        result = self.search_rpc(
            "search.query.v2",
            {
                "owner_id": str(owner_id),
                "query": query,
                "project": None,
                "provider": None,
                "environment": None,
                "window_start_us": None,
                "window_end_us": None,
                "limit": limit,
                "include_snippets": False,
                "include_origin_hidden": False,
            },
        )
        return list(result.get("results") or [])


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


@contextmanager
def provision_live_catalog(
    *,
    instance_id: str = DEFAULT_INSTANCE_ID,
    jwt_secret: str = DEFAULT_JWT_SECRET,
    internal_secret: str = DEFAULT_INTERNAL_SECRET,
) -> Iterator[LiveCatalog]:
    """Run a Runtime Host's live catalog for the duration of the block.

    Everything is per-call: a private temp root, a private database, private
    sockets, and daemons on their own event loop thread. The root is removed on
    exit and every environment and module patch is undone.
    """

    with pytest.MonkeyPatch.context() as monkeypatch:
        root = _live_root()
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{root}/longhouse.db")
        # The switches that make the rest of the suite take legacy branches.
        monkeypatch.setenv("TESTING", "0")
        monkeypatch.setenv("AUTH_DISABLED", "0")
        monkeypatch.setenv("NODE_ENV", "production")
        monkeypatch.setenv("SINGLE_TENANT", "1")
        monkeypatch.setenv("INSTANCE_ID", instance_id)
        monkeypatch.setenv("JWT_SECRET", jwt_secret)
        monkeypatch.setenv("INTERNAL_API_SECRET", internal_secret)
        # Password auth satisfies the production configuration gate without
        # requiring Google OAuth credentials.
        monkeypatch.setenv("LONGHOUSE_PASSWORD", "live-catalog-harness-password")
        monkeypatch.setenv("FERNET_SECRET", Fernet.generate_key().decode())
        monkeypatch.setenv("LONGHOUSE_STORAGE_V2_ROOT", str(root / "objects-v2"))
        monkeypatch.delenv("CONTROL_PLANE_URL", raising=False)
        monkeypatch.delenv("LONGHOUSE_TOOL_STUBS_PATH", raising=False)

        # The signing key is read once at import; align the module constants
        # with the environment so issuing and validating agree in this process.
        monkeypatch.setattr(session_tokens, "JWT_SECRET", jwt_secret)
        monkeypatch.setattr(managed_tokens, "JWT_SECRET", jwt_secret)

        settings = get_settings_unchecked()
        assert settings.testing is False
        assert settings.auth_disabled is False
        assert settings.live_database_url, "the live catalog needs a file-backed database"

        # zerg.database and the auth dependency both cache settings at import.
        # Refresh those copies so live_catalog_enabled() and the strategy
        # selector answer the way they answer on a Runtime Host; monkeypatch
        # restores them.
        monkeypatch.setattr(database_module, "_settings", settings)
        monkeypatch.setattr(auth_deps, "_settings", settings)
        monkeypatch.setattr(auth_deps, "AUTH_DISABLED", False)
        monkeypatch.setattr(auth_deps, "_strategy_cache", {})

        live_database_path, catalog_socket = catalogd_supervisor.catalogd_paths()
        search_socket = root / "searchd.sock"

        loop = _DaemonLoop()
        catalog_daemon = CatalogDaemon(database_path=live_database_path, socket_path=catalog_socket)
        search_daemon = SearchDaemon(database_path=root / "search.db", socket_path=search_socket)
        loop.run(catalog_daemon.start())
        loop.run(search_daemon.start())

        monkeypatch.setattr(catalogd_supervisor, "_supervisor", _PerCallSupervisor(catalog_socket))
        monkeypatch.setattr(searchd_supervisor, "_supervisor", _PerCallSupervisor(search_socket))
        _widen_catalog_read_budgets(monkeypatch)
        _floor_client_rpc_timeouts(monkeypatch)
        # The storage worker pools are process-wide and capture the object root
        # and an event loop when first built. Each catalog owns a different
        # root, so they are rebuilt per catalog and torn down with it.
        _reset_storage_worker_pools()

        try:
            yield LiveCatalog(root, loop, catalog_socket, search_socket, instance_id)
        finally:
            loop.run(raw_object_workers.close_raw_object_worker_pool())
            loop.run(render_object_workers.close_render_object_worker_pool())
            loop.run(search_daemon.close())
            loop.run(catalog_daemon.close())
            loop.close()
            _remove_tree(root)


@pytest.fixture()
def live_catalog() -> Iterator[LiveCatalog]:
    """A live catalog for one test: daemons up, ``live_catalog_enabled()`` true."""

    with provision_live_catalog() as catalog:
        yield catalog


@pytest.fixture()
def live_catalog_client(live_catalog: LiveCatalog) -> Iterator[TestClient]:
    """``api_app`` against the ``live_catalog`` fixture, shaped like production."""

    with live_catalog.http_client() as client:
        yield client
