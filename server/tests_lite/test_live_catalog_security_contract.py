"""The security contract, asserted against a real live catalog.

Every other suite in ``tests_lite`` runs with ``TESTING=1`` and no live store.
That combination flips three switches at once: ``settings.testing`` makes
``_legacy_auth_allowed`` true so authentication resolves users through
SQLAlchemy instead of catalogd, ``live_catalog_enabled()`` returns false so
routes take their archive branches, and the machine-token dependency skips
catalogd entirely. Production has all three the other way round. That is how
two rounds of fixes passed thousands of tests and still shipped a hook-scope
guard on a branch production never takes, an ownership check the production
loader ignored, and a deletion service calling RPCs that did not exist.

So this file provisions the real thing: a ``CatalogDaemon`` over a real Unix
socket, a ``SearchDaemon`` beside it, content-addressed objects on disk, and an
environment shaped the way a self-hosted Runtime Host is shaped -- no
``TESTING``, no ``AUTH_DISABLED``, a file-backed ``DATABASE_URL`` whose live
sibling the daemon owns. Requests go through ``api_app``; every authorization
decision below runs unmocked, over RPC, against real SQL.

Two seams are set rather than mocked, and only because Python binds them at
import time and this process imported them under the test environment:

* the ``db`` dependencies FastAPI captured in the route signatures are forced
  to ``None`` -- exactly the value a live-catalog Runtime Host binds them to;
* ``catalogd_supervisor._supervisor`` and its searchd twin are pointed at the
  daemons this test started, since nothing here runs the real supervisors.

Nothing else is stubbed. Each test states the guard it pins; deleting that
guard fails the test.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

# Import-time defaults only. They exist so importing ``zerg.main`` cannot fail
# when this file is collected first; every test below sets the environment it
# actually depends on through ``monkeypatch``, so nothing here depends on which
# module imported first.
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
from zerg.main import api_app  # noqa: E402
from zerg.routers import agents_sessions as agents_sessions_router  # noqa: E402
from zerg.routers import session_chat as session_chat_router  # noqa: E402
from zerg.searchd.server import SearchDaemon  # noqa: E402
from zerg.services import catalogd_supervisor  # noqa: E402
from zerg.services import data_deletion  # noqa: E402
from zerg.services import raw_object_workers  # noqa: E402
from zerg.services import render_object_workers  # noqa: E402
from zerg.services import searchd_supervisor  # noqa: E402
from zerg.services.data_deletion import SessionNotFound  # noqa: E402
from zerg.services.data_deletion import delete_session_data  # noqa: E402
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

RPC_TIMEOUT_SECONDS = 15.0
INSTANCE_A = "instance-a"
INSTANCE_B = "instance-b"
# The tenant a Runtime Host writes under is derived from INSTANCE_ID, so direct
# catalog commits have to use the same one the HTTP ingest route computes.
TENANT = INSTANCE_A
# Long enough to satisfy the production configuration gate, which refuses to
# start with a weak or default signing key when auth is enabled.
TEST_JWT_SECRET = "live-catalog-contract-jwt-secret-0123456789"
TEST_INTERNAL_SECRET = "live-catalog-contract-internal-secret-0123456789"
PARSER_REVISION = "live-catalog-contract-v1"


# ---------------------------------------------------------------------------
# Harness
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


class _RenderReader:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def read(self, object_path: str, object_hash: str, *, lane: str):
        return read_render_object(self.root, object_path, expected_object_hash=object_hash)


class _RawReader:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def read(self, object_path: str, object_hash: str, tenant_id: str):
        return read_raw_object(self.root, object_path, expected_object_hash=object_hash)


class _PerCallSupervisor:
    """Daemon supervisor stand-in that hands out a fresh client per call.

    ``CatalogClient`` binds its admission semaphore to the first event loop that
    uses it, and this test drives one socket from two loops (the daemons' and
    the TestClient's portal). One client per call keeps that honest without
    changing a byte of the RPC path.
    """

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = socket_path

    @property
    def client(self) -> CatalogClient:
        return CatalogClient(self._socket_path, default_timeout_seconds=RPC_TIMEOUT_SECONDS)

    @property
    def projector_client(self) -> CatalogClient:
        return self.client


@dataclass(frozen=True)
class SeededSession:
    session_id: UUID
    owner_id: int
    envelope_id: str
    raw_path: str
    render_path: str


class LiveCatalog:
    """A real live catalog: catalogd, searchd, and objects on disk."""

    def __init__(self, root: Path, loop: _DaemonLoop, catalog_socket: Path, search_socket: Path) -> None:
        self.root = root
        self.object_root = root / "objects-v2"
        self.loop = loop
        self.catalog_socket = catalog_socket
        self.search_socket = search_socket
        self._catalog = CatalogClient(catalog_socket, default_timeout_seconds=RPC_TIMEOUT_SECONDS)
        self._search = CatalogClient(search_socket, default_timeout_seconds=RPC_TIMEOUT_SECONDS)

    # -- raw RPC ---------------------------------------------------------

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.loop.run(self._catalog.call(method, params or {}))

    def search_rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.loop.run(self._search.call(method, params or {}))

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
                tenant_id=TENANT,
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
                "tenant_id": TENANT,
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
            tenant_id=TENANT,
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
            "tenant_id": TENANT,
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
            worker_id="live-catalog-contract",
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


def _live_root() -> Path:
    # /tmp stays short after macOS resolves it, which keeps the Unix socket
    # path under the 104-byte limit catalogd_paths() also works around.
    root = Path("/tmp") / f"lh-seccontract-{uuid4().hex[:10]}"
    root.mkdir(mode=0o700)
    return root


def _reset_storage_worker_pools() -> None:
    raw_object_workers._pool = None
    render_object_workers._pool = None


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_dir():
            child.rmdir()
        else:
            child.unlink(missing_ok=True)
    path.rmdir()


@pytest.fixture()
def live(monkeypatch):
    """A Runtime Host shaped the way production shapes one."""

    root = _live_root()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{root}/longhouse.db")
    # The switches that make the rest of the suite take legacy branches.
    monkeypatch.setenv("TESTING", "0")
    monkeypatch.setenv("AUTH_DISABLED", "0")
    monkeypatch.setenv("NODE_ENV", "production")
    monkeypatch.setenv("SINGLE_TENANT", "1")
    monkeypatch.setenv("INSTANCE_ID", INSTANCE_A)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("INTERNAL_API_SECRET", TEST_INTERNAL_SECRET)
    # Password auth satisfies the production configuration gate without
    # requiring Google OAuth credentials.
    monkeypatch.setenv("LONGHOUSE_PASSWORD", "live-catalog-contract-password")
    monkeypatch.setenv("FERNET_SECRET", Fernet.generate_key().decode())
    monkeypatch.setenv("LONGHOUSE_STORAGE_V2_ROOT", str(root / "objects-v2"))
    monkeypatch.delenv("CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("LONGHOUSE_TOOL_STUBS_PATH", raising=False)

    # The signing key is read once at import; align the module constants with
    # the environment so issuing and validating agree in this process.
    monkeypatch.setattr(session_tokens, "JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setattr(managed_tokens, "JWT_SECRET", TEST_JWT_SECRET)

    settings = get_settings_unchecked()
    assert settings.testing is False
    assert settings.auth_disabled is False
    assert settings.live_database_url, "the live catalog needs a file-backed database"

    # zerg.database and the auth dependency both cache settings at import.
    # Refresh those copies so live_catalog_enabled() and the strategy selector
    # answer the way they answer on a Runtime Host; monkeypatch restores them.
    monkeypatch.setattr(database_module, "_settings", settings)
    monkeypatch.setattr(auth_deps, "_settings", settings)
    monkeypatch.setattr(auth_deps, "AUTH_DISABLED", False)
    monkeypatch.setattr(auth_deps, "_strategy_cache", {})
    assert database_module.live_catalog_enabled() is True

    from zerg.services.catalogd_supervisor import catalogd_paths

    live_database_path, catalog_socket = catalogd_paths()
    search_socket = root / "searchd.sock"

    loop = _DaemonLoop()
    catalog_daemon = CatalogDaemon(database_path=live_database_path, socket_path=catalog_socket)
    search_daemon = SearchDaemon(database_path=root / "search.db", socket_path=search_socket)
    loop.run(catalog_daemon.start())
    loop.run(search_daemon.start())

    monkeypatch.setattr(catalogd_supervisor, "_supervisor", _PerCallSupervisor(catalog_socket))
    monkeypatch.setattr(searchd_supervisor, "_supervisor", _PerCallSupervisor(search_socket))
    # The storage worker pools are process-wide and capture the object root and
    # an event loop when first built. Each test owns a different root, so they
    # are rebuilt per test and torn down with it.
    _reset_storage_worker_pools()

    harness = LiveCatalog(root, loop, catalog_socket, search_socket)
    try:
        yield harness
    finally:
        loop.run(raw_object_workers.close_raw_object_worker_pool())
        loop.run(render_object_workers.close_render_object_worker_pool())
        loop.run(search_daemon.close())
        loop.run(catalog_daemon.close())
        loop.close()
        _remove_tree(root)


@pytest.fixture()
def client(live):
    """``api_app`` with its import-time db dependencies forced to production's."""

    overrides = {
        session_chat_router._catalog_control_db_dependency: lambda: None,
        agents_sessions_router.session_detail_db_dependency: lambda: None,
        auth_deps._auth_compat_db: lambda: None,
    }
    for dependency, override in overrides.items():
        api_app.dependency_overrides[dependency] = override
    try:
        with TestClient(api_app) as test_client:
            yield test_client
    finally:
        for dependency in overrides:
            api_app.dependency_overrides.pop(dependency, None)


# ---------------------------------------------------------------------------
# 1. Authentication and handoff
# ---------------------------------------------------------------------------


def test_a_managed_machine_token_cannot_be_handed_off_as_a_browser_session(live, client):
    """Guard: browser auth requires the session token kind.

    Managed-session tokens are signed with the same ``JWT_SECRET``, so before
    the kind claim existed, stripping four characters of prefix turned a
    machine credential into a full owner session.
    """

    owner = live.create_user("owner@contract.test")
    cookie = session_tokens._issue_access_token(owner, "owner@contract.test")

    accepted = client.get("/users/me", cookies={"longhouse_session": cookie})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["id"] == owner

    machine_token = managed_tokens.issue_managed_session_token(
        owner_id=owner,
        session_id=str(uuid4()),
        project="longhouse",
        device_id="cinder",
        scope=managed_tokens.MANAGED_SESSION_SCOPE_HOOK,
    )
    stripped = machine_token[len(managed_tokens.MANAGED_SESSION_TOKEN_PREFIX) :]

    as_cookie = client.get("/users/me", cookies={"longhouse_session": stripped})
    as_bearer = client.get("/users/me", headers={"Authorization": f"Bearer {stripped}"})

    assert as_cookie.status_code == 401, as_cookie.text
    assert as_bearer.status_code == 401, as_bearer.text
    # The stripped token is otherwise perfect: same secret, same subject,
    # unexpired, and still valid as what it actually is. Only the kind claim
    # separates it from the accepted cookie above.
    assert managed_tokens.validate_managed_session_token(machine_token) is not None


# ---------------------------------------------------------------------------
# 2. Cross-tenant token rejection
# ---------------------------------------------------------------------------


def test_a_machine_token_minted_for_one_instance_is_refused_by_another(live, client, monkeypatch):
    """Guard: managed-session tokens are bound to the issuing instance.

    Hosted tenants share one ``JWT_SECRET``, so without an audience a token
    minted in any tenant verified in every tenant.
    """

    owner = live.create_user("owner@contract.test")
    seeded = live.commit_session(owner_id=owner, project="longhouse")

    minted_for_a = managed_tokens.issue_managed_session_token(
        owner_id=owner,
        session_id=str(seeded.session_id),
        project="longhouse",
        device_id="cinder",
        scope=managed_tokens.MANAGED_SESSION_SCOPE_HOOK,
    )
    query = {"project": "longhouse", "limit": 5, "days_back": 7}
    accepted = client.get("/agents/sessions", params=query, headers={"X-Agents-Token": minted_for_a})
    assert accepted.status_code == 200, accepted.text

    # Same process, same secret, same token. Only the instance identity moved,
    # which is exactly the hosted-tenant boundary.
    monkeypatch.setenv("INSTANCE_ID", INSTANCE_B)
    refused = client.get("/agents/sessions", params=query, headers={"X-Agents-Token": minted_for_a})

    assert refused.status_code == 401, refused.text
    assert managed_tokens.validate_managed_session_token(minted_for_a) is None
    # Instance B can still mint and accept its own, so this is a binding
    # failure rather than a token that stopped working everywhere.
    minted_for_b = managed_tokens.issue_managed_session_token(
        owner_id=owner,
        session_id=str(seeded.session_id),
        project="longhouse",
        device_id="cinder",
        scope=managed_tokens.MANAGED_SESSION_SCOPE_HOOK,
    )
    assert managed_tokens.validate_managed_session_token(minted_for_b) is not None


# ---------------------------------------------------------------------------
# 3. Machine-control ownership
# ---------------------------------------------------------------------------


def test_a_device_token_cannot_steer_a_session_another_owner_owns(live, client):
    """Guard: every live-control load is owner-scoped, and denial is a 404.

    The load used to be unscoped, so any valid device token on the host could
    send text to, interrupt, or terminate another user's session, and could
    tell a real session id from a made-up one.
    """

    owner = live.create_user("owner@contract.test")
    intruder = live.create_user("intruder@contract.test")
    seeded = live.commit_session(owner_id=owner, project="longhouse")
    owner_token = live.create_device_token(owner_id=owner, device_id="owner-laptop")
    intruder_token = live.create_device_token(owner_id=intruder, device_id="intruder-laptop")

    session_id = str(seeded.session_id)
    unknown_id = str(uuid4())

    def _control_calls(token: str, target: str) -> dict[str, Any]:
        headers = {"X-Agents-Token": token}
        return {
            "send": client.post(f"/agents/sessions/{target}/send-live", json={"message": "whoami"}, headers=headers),
            "interrupt": client.post(f"/agents/sessions/{target}/interrupt-live", headers=headers),
            "terminate": client.post(f"/agents/sessions/{target}/terminate-live", headers=headers),
            "pauses": client.get(f"/agents/sessions/{target}/pause-requests", headers=headers),
        }

    intruder_on_owned = _control_calls(intruder_token, session_id)
    intruder_on_unknown = _control_calls(intruder_token, unknown_id)
    owner_on_owned = _control_calls(owner_token, session_id)

    for action, response in intruder_on_owned.items():
        assert response.status_code == 404, f"{action}: {response.text}"
        assert response.json()["detail"] == f"Session {session_id} not found"
    for action, response in intruder_on_unknown.items():
        assert response.status_code == 404, f"{action}: {response.text}"
        assert response.json()["detail"] == f"Session {unknown_id} not found"

    # Without this the test would pass on a host where every control route
    # 404s. The owner gets past the load and reaches a real answer: the session
    # has no attached control channel, so steering stops at the capability
    # check, and the pause listing simply answers.
    assert [owner_on_owned[action].status_code for action in ("send", "interrupt", "terminate")] == [409, 409, 409], {
        action: owner_on_owned[action].text for action in ("send", "interrupt", "terminate")
    }
    assert owner_on_owned["pauses"].status_code == 200, owner_on_owned["pauses"].text


# ---------------------------------------------------------------------------
# 4. Managed hook-token scoping
# ---------------------------------------------------------------------------


def test_a_hook_token_cannot_widen_its_scope_to_an_owner_wide_read(live, client):
    """Guard: the hook-scope bound runs before the read backend is chosen.

    This guard existed once already and never ran: it sat inside the archive
    listing use case, below the live-catalog branch that returns first. It also
    treated a token with no project as matching a request with no project,
    which is an owner-wide listing carrying summaries and first user messages.
    """

    owner = live.create_user("owner@contract.test")
    seeded = live.commit_session(owner_id=owner, project="longhouse", texts=("an api key lives in this transcript",))
    live.index_search()

    scoped = managed_tokens.issue_managed_session_token(
        owner_id=owner,
        session_id=str(seeded.session_id),
        project="longhouse",
        device_id="cinder",
        scope=managed_tokens.MANAGED_SESSION_SCOPE_HOOK,
    )
    projectless = managed_tokens.issue_managed_session_token(
        owner_id=owner,
        session_id=str(seeded.session_id),
        project=None,
        device_id="cinder",
        scope=managed_tokens.MANAGED_SESSION_SCOPE_HOOK,
    )
    assert managed_tokens.validate_managed_session_token(projectless).project is None

    # The bounded lookup the scope exists to allow still works.
    allowed = client.get(
        "/agents/sessions",
        params={"project": "longhouse", "limit": 5, "days_back": 7},
        headers={"X-Agents-Token": scoped},
    )
    assert allowed.status_code == 200, allowed.text

    # An owner-wide content search is the read this bound exists to refuse.
    searched = client.get(
        "/agents/sessions",
        params={"project": "longhouse", "query": "api key", "limit": 5, "days_back": 7},
        headers={"X-Agents-Token": scoped},
    )
    assert searched.status_code == 403, searched.text

    # Absence is not a match: a token carrying no project must not satisfy the
    # project check by asking for no project.
    unscoped = client.get(
        "/agents/sessions",
        params={"limit": 5, "days_back": 7},
        headers={"X-Agents-Token": projectless},
    )
    assert unscoped.status_code == 403, unscoped.text

    # And a scoped token cannot reach a different project by naming one.
    elsewhere = client.get(
        "/agents/sessions",
        params={"project": "other-project", "limit": 5, "days_back": 7},
        headers={"X-Agents-Token": scoped},
    )
    assert elsewhere.status_code == 403, elsewhere.text


# ---------------------------------------------------------------------------
# 5. Ingest ownership
# ---------------------------------------------------------------------------


def test_a_valid_token_cannot_ship_a_transcript_into_another_owners_account(live, client):
    """Guard: ownership is decided inside the commit transaction.

    Owner identity comes from the token, never the body, and a session already
    bound to somebody else refuses the write rather than being claimed or
    rewritten by it.
    """

    owner = live.create_user("owner@contract.test")
    intruder = live.create_user("intruder@contract.test")
    seeded = live.commit_session(owner_id=owner, project="longhouse", texts=("the owner's own transcript",))
    intruder_token = live.create_device_token(owner_id=intruder, device_id="intruder-laptop")

    response = client.post(
        "/agents/storage/v2/envelopes",
        json=live.envelope_body(
            session_id=seeded.session_id,
            device_id="intruder-laptop",
            texts=("injected by somebody else",),
        ),
        headers={"X-Agents-Token": intruder_token, "X-Longhouse-Storage-Lane": "live"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["details"] == {"reason": "session_owner_conflict"}

    # The binding did not move, and the intruder still cannot read the session.
    facts = live.rpc("storage.session.read.v2", {"session_id": str(seeded.session_id)})
    assert str(facts["session"]["owner_id"]) == str(owner)
    intruder_read = client.get(
        f"/agents/storage/v2/sessions/{seeded.session_id}/raw",
        headers={"X-Agents-Token": intruder_token},
    )
    assert intruder_read.status_code == 404, intruder_read.text

    # The same envelope into the intruder's own session is accepted, so the
    # refusal above is about ownership and not about the payload.
    own_session = uuid4()
    accepted = client.post(
        "/agents/storage/v2/envelopes",
        json=live.envelope_body(
            session_id=own_session,
            device_id="intruder-laptop",
            texts=("injected by somebody else",),
        ),
        headers={"X-Agents-Token": intruder_token, "X-Longhouse-Storage-Lane": "live"},
    )
    assert accepted.status_code == 200, accepted.text
    own_facts = live.rpc("storage.session.read.v2", {"session_id": str(own_session)})
    assert str(own_facts["session"]["owner_id"]) == str(intruder)


# ---------------------------------------------------------------------------
# 6. Search and export scoping
# ---------------------------------------------------------------------------


def test_search_and_export_are_owner_scoped_inside_the_query(live, client):
    """Guard: the owner predicate is part of the SQL, not a filter afterwards.

    A post-filter is indistinguishable from an in-query predicate until the
    result set is bounded. Here the other owner's session outranks this one, so
    a ``LIMIT 1`` applied before the owner check returns nothing at all.
    """

    owner = live.create_user("owner@contract.test")
    other = live.create_user("other@contract.test")
    term = "photonic"
    mine = live.commit_session(
        owner_id=owner,
        project="longhouse",
        texts=(f"a long transcript line that mentions {term} exactly once among many other unrelated words",),
    )
    theirs = live.commit_session(
        owner_id=other,
        project="other-project",
        texts=(f"{term} {term} {term}",),
    )
    live.index_search()

    mine_rows = live.search(owner_id=owner, query=term, limit=10)
    theirs_rows = live.search(owner_id=other, query=term, limit=10)
    assert [row["session_id"] for row in mine_rows] == [str(mine.session_id)]
    assert [row["session_id"] for row in theirs_rows] == [str(theirs.session_id)]

    # bm25 ranks lower-is-better. The other owner's row wins globally, so a
    # top-1 taken before the owner predicate would hold their row and drop it.
    assert min(float(row["rank"]) for row in theirs_rows) < min(float(row["rank"]) for row in mine_rows)
    bounded = live.search(owner_id=owner, query=term, limit=1)
    assert [row["session_id"] for row in bounded] == [str(mine.session_id)]

    # The machine search route carries the same scope end to end.
    owner_token = live.create_device_token(owner_id=owner, device_id="owner-laptop")
    searched = client.get(
        "/agents/sessions",
        params={"query": term, "limit": 20, "days_back": 7},
        headers={"X-Agents-Token": owner_token},
    )
    assert searched.status_code == 200, searched.text
    assert [session["id"] for session in searched.json()["sessions"]] == [str(mine.session_id)]

    # Export is scoped by the same identity: the raw transcript of a session
    # this owner does not own is reported as absent.
    own_raw = client.get(f"/agents/storage/v2/sessions/{mine.session_id}/raw", headers={"X-Agents-Token": owner_token})
    assert own_raw.status_code == 200, own_raw.text
    denied = client.get(
        f"/agents/storage/v2/sessions/{theirs.session_id}/raw",
        headers={"X-Agents-Token": owner_token},
    )
    assert denied.status_code == 404, denied.text


# ---------------------------------------------------------------------------
# 7. Deletion
# ---------------------------------------------------------------------------


def test_deletion_removes_catalog_rows_search_entries_and_blob_bytes(live, monkeypatch):
    """Guard: deletion reaches every store, and denial is not an existence oracle.

    Deletion previously fenced manifests while leaving readable content in the
    search index and the bytes on disk, and answered a non-owner in a way that
    distinguished "someone else's" from "never existed".
    """

    owner = live.create_user("owner@contract.test")
    stranger = live.create_user("stranger@contract.test")
    term = "quasar"
    doomed = live.commit_session(owner_id=owner, texts=(f"the {term} secret",))
    kept = live.commit_session(owner_id=owner, texts=(f"another {term} line",))
    live.index_search()

    catalog_client = CatalogClient(live.catalog_socket, default_timeout_seconds=RPC_TIMEOUT_SECONDS)
    search_client = CatalogClient(live.search_socket, default_timeout_seconds=RPC_TIMEOUT_SECONDS)
    monkeypatch.setattr(data_deletion, "get_catalogd_client", lambda: catalog_client)
    monkeypatch.setattr(data_deletion, "get_searchd_client", lambda: search_client)

    raw_file = live.object_root / doomed.raw_path
    render_file = live.object_root / doomed.render_path
    assert raw_file.exists() and render_file.exists()
    indexed = {row["session_id"] for row in live.search(owner_id=owner, query=term, limit=10)}
    assert indexed == {str(doomed.session_id), str(kept.session_id)}

    report = live.loop.run(delete_session_data(session_id=doomed.session_id, owner_id=owner))

    assert report.search_index_removed is True
    assert report.raw_objects_deleted == 1
    assert report.render_objects_deleted == 1
    assert report.manifest_rows_retired >= 2
    # The bytes are gone from disk, not merely fenced from serving.
    assert not raw_file.exists()
    assert not render_file.exists()
    # The catalog rows are gone from the owner's served listing, not only
    # fenced behind a tombstone, and the index no longer holds the readable
    # content -- while the neighbouring session is untouched throughout.
    assert live.rpc("storage.session.read.v2", {"session_id": str(doomed.session_id)})["found"] is False
    timeline = live.rpc(
        "storage.session.timeline.list.v2",
        {
            "owner_id": str(owner),
            "before_last_activity_at": None,
            "before_session_id": None,
            "project": None,
            "provider": None,
            "include_test": False,
            "limit": 100,
        },
    )
    assert [row["session_id"] for row in timeline["sessions"]] == [str(kept.session_id)]
    assert {row["session_id"] for row in live.search(owner_id=owner, query=term, limit=10)} == {str(kept.session_id)}
    assert (live.object_root / kept.raw_path).exists()

    # A stranger gets one indistinguishable answer for "deleted", "somebody
    # else's" and "never existed", so a guessed UUID is not an existence oracle.
    never_existed = uuid4()
    outcomes = []
    for probe in (never_existed, kept.session_id, doomed.session_id):
        with pytest.raises(SessionNotFound) as error:
            live.loop.run(delete_session_data(session_id=probe, owner_id=stranger))
        outcomes.append((type(error.value), error.value.args))
    assert outcomes[0][0] is outcomes[1][0] is outcomes[2][0]
    assert [args for _, args in outcomes] == [(str(never_existed),), (str(kept.session_id),), (str(doomed.session_id),)]

    # Only the owner learns the terminal state.
    assert live.loop.run(delete_session_data(session_id=doomed.session_id, owner_id=owner)).already_deleted is True
