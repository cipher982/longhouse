"""Managed local launch, asserted against a real live catalog.

This file used to run every launch through the legacy branch: no catalogd, a
request-scoped SQLAlchemy session, readiness rows in a test-owned Live Store
and an archive outbox to drain. A Runtime Host takes none of that path. It
hands the launch to catalogd, which owns the launch attempt, the run, the
control connection and readiness, while the API process holds no SQLite
session at all.

So the launch tests below provision the real thing through
``live_catalog_harness``: a ``CatalogDaemon`` over a real Unix socket, an
environment shaped the way a self-hosted Runtime Host shapes it, and a real
device token on every request. What the daemon durably holds is read back
through ``load_live_control_session_snapshot`` -- the same bounded gateway the
Runtime Host reads control facts through -- rather than by opening the
catalog's SQLite behind its back.

Plan construction and response-contract validation are pure functions with no
catalog behind them, so those tests stay pure functions.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import provision_live_catalog
from zerg.services.live_control_catalog import load_live_control_session_snapshot
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.managed_local_launcher import _derive_project
from zerg.services.managed_local_launcher import _initial_provider_session_id_for_spawn
from zerg.services.managed_local_launcher import build_managed_local_launch_plan
from zerg.services.managed_local_launcher import managed_local_run_id_for_session

DEVICE_ID = "cinder"
OWNER_EMAIL = "managed-local@test.local"
LAUNCH_PATH = "/sessions/managed-local/this-device"


@pytest.fixture()
def live():
    """A Runtime Host shaped the way production shapes one."""

    with provision_live_catalog() as catalog:
        yield catalog


@pytest.fixture()
def owner_id(live: LiveCatalog) -> int:
    return live.create_user(OWNER_EMAIL)


@pytest.fixture()
def device_headers(live: LiveCatalog, owner_id: int) -> dict[str, str]:
    """The only identity this route accepts: one machine's device token."""

    return {"X-Agents-Token": live.create_device_token(owner_id=owner_id, device_id=DEVICE_ID)}


@pytest.fixture()
def client(live: LiveCatalog):
    """``api_app`` with its import-time db dependencies forced to production's."""

    with live.http_client() as test_client:
        yield test_client


def _launch(client, headers: dict[str, str], **body):
    payload = {"cwd": "/tmp/demo", "provider": "codex", "project": "demo"}
    payload.update(body)
    return client.post(LAUNCH_PATH, json=payload, headers=headers)


def _launch_params(owner: int, **overrides) -> ManagedLocalLaunchParams:
    params = {
        "owner_id": owner,
        "runner_target": DEVICE_ID,
        "cwd": "/tmp/demo",
        "provider": "codex",
        "project": "demo",
        "machine_name": DEVICE_ID,
    }
    params.update(overrides)
    return ManagedLocalLaunchParams(**params)


def _launch_facts(session_id, *, owner: int):
    """Bounded control facts, read back the way the Runtime Host reads them."""

    snapshot = load_live_control_session_snapshot(str(session_id), owner_id=owner)
    return None if snapshot is None else snapshot.catalog_facts


def test_managed_local_derived_project_ignores_generic_workspace():
    assert _derive_project("/private/tmp/longhouse/workspace", None) == "managed-local"
    assert _derive_project("/private/tmp/longhouse/workspace", "explicit") == "explicit"


def test_initial_provider_session_id_for_spawn_is_provider_specific():
    claude_provider_id = _initial_provider_session_id_for_spawn("claude")
    assert claude_provider_id
    assert _initial_provider_session_id_for_spawn("codex") is None
    assert _initial_provider_session_id_for_spawn("opencode") is None
    assert _initial_provider_session_id_for_spawn("antigravity") is None


def test_managed_local_launch_plan_builds_codex_attach_command_without_archive_db():
    plan = build_managed_local_launch_plan(
        ManagedLocalLaunchParams(
            owner_id=1,
            runner_target="cinder",
            cwd="/tmp/demo",
            provider="codex",
            project="demo",
            machine_name="cinder",
        )
    )

    assert plan.provider == "codex"
    assert "codex-bridge attach --session-id" in plan.attach_command
    assert plan.provider_session_id is None
    assert plan.source_name == "cinder"
    assert plan.project == "demo"
    assert plan.managed_transport == "codex_app_server"
    assert str(plan.session_id) in plan.attach_command


def test_explicit_automation_provenance_is_policy_hidden_without_path_heuristics():
    plan = build_managed_local_launch_plan(
        ManagedLocalLaunchParams(
            owner_id=1,
            runner_target="cinder",
            cwd="/tmp/ordinary-looking-workspace",
            provider="codex",
            project="ordinary",
            machine_name="cinder",
            launch_actor="automation",
            launch_surface="ci",
        )
    )

    assert plan.origin_kind is None
    assert (plan.launch_actor, plan.launch_surface) == ("automation", "ci")
    assert plan.hidden_from_default_timeline == 1


def test_managed_local_launch_plan_builds_claude_attach_command_without_archive_db():
    plan = build_managed_local_launch_plan(
        ManagedLocalLaunchParams(
            owner_id=1,
            runner_target="cinder",
            cwd="/tmp/demo",
            provider="claude",
            project="demo",
            machine_name="cinder",
            native_claude_channels_available=True,
        )
    )

    assert plan.provider == "claude"
    assert plan.provider_session_id
    assert plan.provider_session_id in plan.attach_command
    assert "LONGHOUSE_PROVIDER_SESSION_ID" in plan.attach_command
    assert str(plan.session_id) in plan.attach_command
    assert plan.managed_transport == "claude_channel_bridge"


def test_managed_local_launch_plan_preserves_client_minted_provider_identity():
    provider_session_id = "11111111-1111-4111-8111-111111111111"
    plan = build_managed_local_launch_plan(
        ManagedLocalLaunchParams(
            owner_id=1,
            runner_target="cinder",
            cwd="/tmp/demo",
            provider="claude",
            machine_name="cinder",
            native_claude_channels_available=True,
            provider_session_id=provider_session_id,
        )
    )

    assert plan.provider_session_id == provider_session_id


def test_managed_local_launch_response_contract_rejects_missing_claude_provider_id():
    from zerg.services.session_chat_impl import ManagedLocalSessionLaunchResponse
    from zerg.services.session_chat_impl import _validate_managed_local_launch_response_contract
    from zerg.session_execution_home import ManagedSessionTransport
    from zerg.session_execution_home import SessionExecutionHome
    from zerg.session_loop_mode import SessionLoopMode

    response = ManagedLocalSessionLaunchResponse(
        session_id="session-123",
        run_id="11111111-1111-4111-8111-111111111111",
        provider="claude",
        provider_session_id=None,
        execution_home=SessionExecutionHome.MANAGED_LOCAL,
        managed_transport=ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE,
        loop_mode=SessionLoopMode.ASSIST,
        source_runner_id=1,
        source_runner_name="cinder",
        managed_session_name="demo",
        attach_command="",
    )

    with pytest.raises(RuntimeError, match="missing provider_session_id"):
        _validate_managed_local_launch_response_contract(
            session_id="session-123",
            response=response,
        )


def test_managed_local_launch_response_contract_accepts_pi_print_without_attach_command():
    from zerg.services.session_chat_impl import ManagedLocalSessionLaunchResponse
    from zerg.services.session_chat_impl import _validate_managed_local_launch_response_contract
    from zerg.session_execution_home import ManagedSessionTransport
    from zerg.session_execution_home import SessionExecutionHome
    from zerg.session_loop_mode import SessionLoopMode

    response = ManagedLocalSessionLaunchResponse(
        session_id="session-pi-1",
        run_id="22222222-2222-4222-8222-222222222222",
        provider="pi",
        provider_session_id=None,
        execution_home=SessionExecutionHome.MANAGED_LOCAL,
        managed_transport=ManagedSessionTransport.PI_PRINT,
        loop_mode=SessionLoopMode.ASSIST,
        source_runner_id=1,
        source_runner_name="cinder",
        managed_session_name="pi-demo",
        attach_command="",
    )

    # The one-shot pi_print transport has no attach/resume command; an empty
    # attach_command must pass the response contract (regression: this used to
    # raise "Unsupported managed local launch response transport: pi_print",
    # surfacing as 500 "Managed local launch failed" on launch).
    _validate_managed_local_launch_response_contract(
        session_id="session-pi-1",
        response=response,
    )


def test_managed_local_launch_response_contract_rejects_pi_print_attach_command():
    from zerg.services.session_chat_impl import ManagedLocalSessionLaunchResponse
    from zerg.services.session_chat_impl import _validate_managed_local_launch_response_contract
    from zerg.session_execution_home import ManagedSessionTransport
    from zerg.session_execution_home import SessionExecutionHome
    from zerg.session_loop_mode import SessionLoopMode

    response = ManagedLocalSessionLaunchResponse(
        session_id="session-pi-2",
        run_id="33333333-3333-4333-8333-333333333333",
        provider="pi",
        provider_session_id=None,
        execution_home=SessionExecutionHome.MANAGED_LOCAL,
        managed_transport=ManagedSessionTransport.PI_PRINT,
        loop_mode=SessionLoopMode.ASSIST,
        source_runner_id=1,
        source_runner_name="cinder",
        managed_session_name="pi-demo",
        attach_command="longhouse pi --resume-session session-pi-2",
    )

    with pytest.raises(RuntimeError, match="should not include an attach command"):
        _validate_managed_local_launch_response_contract(
            session_id="session-pi-2",
            response=response,
        )


def test_this_device_launch_discards_session_when_response_contract_fails(monkeypatch, client, device_headers, owner_id):
    """A launch that fails its own response contract registers nothing.

    The contract runs before catalogd is asked to persist anything, so the
    failure must leave no session, run, connection or readiness behind for a
    machine to adopt.
    """

    from zerg.services import managed_local_launcher

    monkeypatch.setattr(
        managed_local_launcher,
        "_initial_provider_session_id_for_spawn",
        lambda _provider: None,
    )
    minted = uuid4()

    response = _launch(
        client,
        device_headers,
        provider="claude",
        session_id=str(minted),
        native_claude_channels_available=True,
    )

    assert response.status_code == 500, response.text
    assert response.json()["detail"] == "Managed local launch failed"
    assert _launch_facts(minted, owner=owner_id) is None


def test_browser_managed_local_launch_route_is_absent():
    from fastapi.testclient import TestClient

    from zerg.main import app

    client = TestClient(app, backend="asyncio")
    response = client.post(
        "/api/sessions/managed-local",
        json={
            "runner_target": "runner:1",
            "cwd": "/tmp/demo",
            "provider": "claude",
        },
    )

    assert response.status_code == 404


def test_this_device_launch_does_not_consult_runner_liveness(monkeypatch, client, device_headers):
    """Starting a provider on its own machine is not gated on runner liveness.

    The legacy path resolved an archive ``Runner`` row and asked the connection
    manager whether it was online. The live-catalog path asks neither, so a
    call into the runner registry here is a regression rather than a detail.
    """

    from zerg.services import managed_local_launcher

    def _refuse_registry():
        raise AssertionError("managed local launch must not consult runner liveness")

    monkeypatch.setattr(managed_local_launcher, "get_runner_connection_manager", _refuse_registry)

    response = _launch(client, device_headers, provider="claude", native_claude_channels_available=True)

    assert response.status_code == 200, response.text
    assert response.json()["managed_transport"] == "claude_channel_bridge"


def test_this_device_launch_refuses_a_body_supplied_machine_identity(client, owner_id):
    """``machine_name`` is a display label, never a credential.

    Device identity comes from the token and nothing else, so an unauthenticated
    request naming a machine is refused and leaves no launch behind.
    """

    minted = uuid4()
    response = client.post(
        LAUNCH_PATH,
        json={
            "cwd": "/tmp/demo",
            "provider": "codex",
            "project": "demo",
            "machine_name": DEVICE_ID,
            "session_id": str(minted),
        },
    )

    assert response.status_code == 401, response.text
    assert _launch_facts(minted, owner=owner_id) is None


def test_this_device_launch_does_not_require_runner_record(client, device_headers):
    response = _launch(client, device_headers, provider="codex")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source_runner_id"] is None
    assert payload["source_runner_name"] == DEVICE_ID
    assert payload["managed_transport"] == "codex_app_server"


def test_this_device_launch_returns_client_minted_identities_unchanged(client, device_headers, owner_id):
    minted = uuid4()
    provider_minted = uuid4()

    response = _launch(
        client,
        device_headers,
        provider="claude",
        session_id=str(minted),
        provider_session_id=str(provider_minted),
        native_claude_channels_available=True,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["session_id"] == str(minted)
    assert payload["provider_session_id"] == str(provider_minted)
    # Catalogd binds the client-minted provider identity to the launch thread,
    # and the route refuses any launch whose durable identity differs from the
    # plan it just returned.
    facts = _launch_facts(minted, owner=owner_id)
    assert facts["provider_alias"] == str(provider_minted)
    assert facts["resume"]["provider_session_id"] == str(provider_minted)


def test_this_device_launch_writes_launch_state_to_catalogd_not_the_api_writer(monkeypatch, client, device_headers, owner_id):
    """Catalogd owns launch state; the API process writes no store of its own."""

    from zerg.routers import session_chat

    monkeypatch.setattr(
        session_chat,
        "get_live_write_serializer",
        lambda: (_ for _ in ()).throw(AssertionError("a live-catalog launch must not use the API live serializer")),
    )

    response = _launch(client, device_headers, provider="codex")

    assert response.status_code == 200, response.text
    payload = response.json()
    facts = _launch_facts(payload["session_id"], owner=owner_id)
    readiness = facts["readiness"]
    assert readiness["state"] == "pending"
    assert readiness["command_id"] == f"managed-local-{payload['session_id']}"
    assert readiness["device_id"] == DEVICE_ID
    assert readiness["provider"] == "codex"
    assert facts["latest_run"]["id"] == payload["run_id"]


def test_managed_local_launch_registers_a_pending_run_in_catalogd(live, owner_id):
    """The run id is deterministic and readiness is claimed before the provider starts."""

    from zerg.routers import session_chat

    result, response = asyncio.run(
        session_chat._launch_managed_local_session_serialized(
            None,
            _launch_params(owner_id, provider="codex"),
        )
    )

    assert result is None
    assert response.provider == "codex"
    assert response.managed_transport.value == "codex_app_server"
    assert "codex-bridge attach --session-id" in response.attach_command
    assert response.session_id in response.attach_command
    assert response.run_id == str(managed_local_run_id_for_session(response.session_id))
    facts = _launch_facts(response.session_id, owner=owner_id)
    assert facts["readiness"]["state"] == "pending"
    assert facts["readiness"]["provider"] == "codex"
    assert facts["readiness"]["device_id"] == DEVICE_ID


@pytest.mark.parametrize(
    ("provider", "expected_transport", "expected_attach"),
    [
        ("cursor", "cursor_helm", "cursor"),
        ("codex", "codex_app_server", "codex"),
        ("antigravity", "antigravity_hook_inbox", ""),
    ],
)
def test_this_device_launch_materializes_live_catalog_without_archive_db(
    monkeypatch,
    live,
    owner_id,
    provider,
    expected_transport,
    expected_attach,
):
    from zerg.routers import session_chat

    monkeypatch.setattr(
        session_chat,
        "get_live_write_serializer",
        lambda: (_ for _ in ()).throw(AssertionError("catalog launch must not use the API live serializer")),
    )

    _result, response = asyncio.run(
        session_chat._launch_managed_local_session_serialized(
            None,
            _launch_params(owner_id, provider=provider),
        )
    )

    assert response.provider == provider
    assert response.managed_transport.value == expected_transport
    if expected_attach == "cursor":
        assert "longhouse cursor --resume-session" in response.attach_command
        assert response.session_id in response.attach_command
    elif expected_attach == "codex":
        assert "codex-bridge attach --session-id" in response.attach_command
        assert response.session_id in response.attach_command
    else:
        assert response.attach_command == ""
    assert response.provider_session_id is None

    facts = _launch_facts(response.session_id, owner=owner_id)
    catalog = facts["catalog"]
    assert catalog["project"] == "demo"
    assert catalog["primary_thread_id"] is not None
    assert facts["primary_thread"]["id"] == catalog["primary_thread_id"]
    assert facts["readiness"]["command_id"] == f"managed-local-{response.session_id}"
    assert facts["readiness"]["state"] == "pending"
    assert facts["latest_run"]["id"] == response.run_id
    (connection,) = facts["connections"]
    assert connection["run_id"] == response.run_id
    assert connection["state"] == "detached"
    assert connection["device_id"] == DEVICE_ID
    if provider == "antigravity":
        assert connection["can_send_input"] == 0
    # No provider thread alias is minted for providers that have no
    # provider-native session identity at spawn.
    assert facts["provider_alias"] is None


def test_this_device_launch_surfaces_catalog_rejection_without_retry_theater(monkeypatch, live, owner_id):
    from zerg.catalogd.client import CatalogRemoteError
    from zerg.catalogd.protocol import CatalogRpcError
    from zerg.routers import session_chat
    from zerg.services.managed_local_launcher import ManagedLocalLaunchError

    class RejectingCatalog:
        async def call(self, method, params, **_kwargs):
            assert method == "session.launch.local.create.v2"
            raise CatalogRemoteError(
                CatalogRpcError(
                    code="invalid_request",
                    message="local launch.plan.attach_command must be a string of at most 4096 characters",
                    retryable=False,
                    retry_after_ms=None,
                    details={},
                )
            )

    monkeypatch.setattr("zerg.services.catalogd_supervisor.get_catalogd_client", lambda: RejectingCatalog())

    with pytest.raises(ManagedLocalLaunchError) as exc_info:
        asyncio.run(
            session_chat._launch_managed_local_session_serialized(
                None,
                _launch_params(owner_id, provider="cursor"),
            )
        )

    assert exc_info.value.status_code == 500
    assert "attach_command" in exc_info.value.detail
    assert "retry shortly" not in exc_info.value.detail.lower()
    assert "unavailable" not in exc_info.value.detail.lower()


@pytest.mark.parametrize(
    ("case", "expected_status", "detail_must_include", "detail_must_exclude"),
    [
        ("unavailable", 503, ("unavailable",), ("attach_command",)),
        ("invalid_request", 500, ("attach_command",), ("retry shortly", "unavailable")),
        ("conflict", 409, ("conflict",), ("retry shortly",)),
        ("unexpected", 500, ("persist",), ("retry shortly", "unavailable")),
    ],
)
def test_managed_local_catalog_error_class_matrix(
    monkeypatch,
    live,
    owner_id,
    case,
    expected_status,
    detail_must_include,
    detail_must_exclude,
):
    """Phase A guard: do not remap contract bugs into fake catalogd-unavailable 503s."""
    from zerg.catalogd.client import CatalogRemoteError
    from zerg.catalogd.client import CatalogUnavailable
    from zerg.catalogd.protocol import CatalogRpcError
    from zerg.routers import session_chat
    from zerg.services.managed_local_launcher import ManagedLocalLaunchError

    if case == "unavailable":
        raised: Exception = CatalogUnavailable("catalogd socket missing")
    elif case == "invalid_request":
        raised = CatalogRemoteError(
            CatalogRpcError(
                code="invalid_request",
                message="local launch.plan.attach_command must be a string of at most 4096 characters",
                retryable=False,
                retry_after_ms=None,
                details={},
            )
        )
    elif case == "conflict":
        raised = CatalogRemoteError(
            CatalogRpcError(
                code="conflict",
                message="managed-local launch identity conflict",
                retryable=False,
                retry_after_ms=None,
                details={},
            )
        )
    else:
        # Whatever else a write can raise is a persistence failure, not a
        # reason to tell the machine that catalogd is down and to retry.
        raised = RuntimeError("catalogd write failed")

    class BoomCatalog:
        async def call(self, method, params, **_kwargs):
            raise raised

    monkeypatch.setattr("zerg.services.catalogd_supervisor.get_catalogd_client", lambda: BoomCatalog())

    with pytest.raises(ManagedLocalLaunchError) as exc_info:
        asyncio.run(
            session_chat._launch_managed_local_session_serialized(
                None,
                _launch_params(owner_id, provider="cursor"),
            )
        )

    assert exc_info.value.status_code == expected_status
    detail = exc_info.value.detail.lower()
    for needle in detail_must_include:
        assert needle in detail
    for needle in detail_must_exclude:
        assert needle not in detail


def test_this_device_launch_skips_runtime_pubsub_for_catalog_launch(monkeypatch, client, device_headers):
    """There is no archive session to project, so nothing is published."""

    from zerg.services import session_pubsub

    publish_calls: list[dict] = []
    monkeypatch.setattr(
        session_pubsub,
        "publish_session_runtime_update",
        lambda **kwargs: publish_calls.append(kwargs),
    )

    response = _launch(client, device_headers, provider="codex")

    assert response.status_code == 200, response.text
    assert response.json()["provider"] == "codex"
    assert publish_calls == []


def test_this_device_launch_validates_response_before_catalog_write(monkeypatch, live, owner_id):
    from zerg.routers import session_chat
    from zerg.services import managed_local_launcher

    monkeypatch.setattr(
        managed_local_launcher,
        "_initial_provider_session_id_for_spawn",
        lambda _provider: None,
    )
    minted = uuid4()

    with pytest.raises(RuntimeError, match="missing provider_session_id"):
        asyncio.run(
            session_chat._launch_managed_local_session_serialized(
                None,
                _launch_params(
                    owner_id,
                    provider="claude",
                    session_id=minted,
                    native_claude_channels_available=True,
                ),
            )
        )

    assert _launch_facts(minted, owner=owner_id) is None


def test_this_device_launch_rejects_claude_without_native_channels(client, device_headers):
    response = _launch(
        client,
        device_headers,
        provider="claude",
        native_claude_channels_available=False,
    )

    assert response.status_code == 412
    assert "requires the local Claude channel bridge" in response.json()["detail"]


def test_this_device_launch_returns_native_claude_hot_launch(client, device_headers, owner_id):
    response = _launch(
        client,
        device_headers,
        provider="claude",
        display_name="Demo session",
        native_claude_channels_available=True,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["managed_transport"] == "claude_channel_bridge"
    assert payload["source_runner_id"] is None
    assert payload["source_runner_name"] == DEVICE_ID
    assert payload["managed_session_name"] == "Demo-session"
    assert payload["provider_session_id"]
    assert payload["provider_session_id"] != payload["session_id"]
    assert f"--session-id {payload['provider_session_id']}" in payload["attach_command"]
    assert f"LONGHOUSE_PROVIDER_SESSION_ID={payload['provider_session_id']}" in payload["attach_command"]
    facts = _launch_facts(payload["session_id"], owner=owner_id)
    assert facts["readiness"]["state"] == "pending"
    assert facts["readiness"]["provider"] == "claude"
    assert facts["provider_alias"] == payload["provider_session_id"]


def test_this_device_launch_uses_token_device_id_not_machine_name(client, device_headers, owner_id):
    response = _launch(
        client,
        device_headers,
        provider="claude",
        display_name="Demo session",
        machine_name="cinder.local",
        native_claude_channels_available=True,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source_runner_name"] == DEVICE_ID
    facts = _launch_facts(payload["session_id"], owner=owner_id)
    assert facts["catalog"]["device_id"] == DEVICE_ID
    assert facts["readiness"]["device_id"] == DEVICE_ID


@pytest.mark.parametrize(
    ("provider", "expected_transport"),
    [
        ("claude", "claude_channel_bridge"),
        ("codex", "codex_app_server"),
        ("opencode", "opencode_server_bridge"),
        ("antigravity", "antigravity_hook_inbox"),
        ("cursor", "cursor_helm"),
    ],
)
def test_this_device_launch_response_contract_matrix(client, device_headers, provider, expected_transport):
    request_payload = {"provider": provider}
    if provider == "claude":
        request_payload["native_claude_channels_available"] = True

    response = _launch(client, device_headers, **request_payload)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["managed_transport"] == expected_transport
    assert payload["source_runner_name"] == DEVICE_ID
    assert payload["provider"] == provider
    # Cursor Helm refuses to launch without one, so this list is load-bearing
    # rather than descriptive.
    if provider in {"claude", "codex", "opencode", "cursor"}:
        assert payload["coordination_token"]
    else:
        assert payload["coordination_token"] is None

    if provider == "claude":
        assert payload["provider_session_id"]
        assert payload["provider_session_id"] != payload["session_id"]
        assert f"--session-id {payload['provider_session_id']}" in payload["attach_command"]
        assert f"LONGHOUSE_PROVIDER_SESSION_ID={payload['provider_session_id']}" in payload["attach_command"]
    elif provider == "codex":
        assert payload["provider_session_id"] is None
        assert "codex-bridge attach --session-id" in payload["attach_command"]
        assert payload["session_id"] in payload["attach_command"]
    elif provider == "opencode":
        assert payload["provider_session_id"] is None
        assert "opencode-channel attach --session-id" in payload["attach_command"]
        assert payload["session_id"] in payload["attach_command"]
    elif provider == "antigravity":
        assert payload["provider_session_id"] is None
        assert payload["attach_command"] == ""
    elif provider == "cursor":
        assert payload["provider_session_id"] is None
        assert "longhouse cursor --resume-session" in payload["attach_command"]
        assert payload["session_id"] in payload["attach_command"]


def test_launch_outcome_uses_device_bound_catalog_transaction(live, client, device_headers, owner_id):
    """The launching device is the only one that can finish its own launch.

    Device identity comes from the token, never from the request body, and
    catalogd checks it against the durable launch attempt rather than trusting
    the caller.
    """

    launch = _launch(client, device_headers, provider="codex")
    assert launch.status_code == 200, launch.text
    session_id = launch.json()["session_id"]
    run_id = launch.json()["run_id"]

    other_device = {"X-Agents-Token": live.create_device_token(owner_id=owner_id, device_id="basalt")}
    stolen = client.post(
        f"/agents/sessions/{session_id}/launch-outcome",
        json={"run_id": run_id, "outcome": "confirmed"},
        headers=other_device,
    )
    assert stolen.status_code == 409, stolen.text
    assert _launch_facts(session_id, owner=owner_id)["readiness"]["state"] == "pending"

    confirmed = client.post(
        f"/agents/sessions/{session_id}/launch-outcome",
        json={
            "run_id": run_id,
            "outcome": "confirmed",
            "error_code": "ignored_on_confirm",
            "error_message": "ignored on confirm",
        },
        headers=device_headers,
    )

    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json() == {
        "recorded": True,
        "session_id": session_id,
        "run_id": run_id,
        "launch_state": "live",
        "error_code": None,
    }
    assert _launch_facts(session_id, owner=owner_id)["readiness"]["state"] == "adopted"


def test_launch_outcome_for_an_unregistered_launch_is_not_found(client, device_headers):
    response = client.post(
        f"/agents/sessions/{uuid4()}/launch-outcome",
        json={"run_id": str(uuid4()), "outcome": "confirmed"},
        headers=device_headers,
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Launch was not found"


def test_this_device_launch_returns_native_codex_hot_launch(client, device_headers, owner_id):
    response = _launch(client, device_headers, provider="codex")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["managed_transport"] == "codex_app_server"
    assert payload["source_runner_id"] is None
    assert '"$engine" codex-bridge attach --session-id' in payload["attach_command"]
    facts = _launch_facts(payload["session_id"], owner=owner_id)
    assert facts["readiness"]["state"] == "pending"
    assert facts["catalog"]["cwd"] == "/tmp/demo"
    assert facts["catalog"]["project"] == "demo"


def test_this_device_launch_returns_native_antigravity_hot_launch(client, device_headers, owner_id):
    response = _launch(client, device_headers, provider="antigravity")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["managed_transport"] == "antigravity_hook_inbox"
    assert payload["source_runner_id"] is None
    assert payload["attach_command"] == ""
    assert payload["provider"] == "antigravity"
    # Antigravity has no served input surface at launch: the control connection
    # is registered without send authority rather than optimistically granted.
    (connection,) = _launch_facts(payload["session_id"], owner=owner_id)["connections"]
    assert connection["can_send_input"] == 0
