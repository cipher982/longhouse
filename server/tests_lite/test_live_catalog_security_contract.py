"""The security contract, asserted against a real live catalog.

The rest of ``tests_lite`` runs with no live store behind it, so a route that
reaches for catalogd finds nothing there. Production has a daemon, a live
database and real authentication on every call. That gap is how two rounds of
fixes passed thousands of tests and still shipped a hook-scope guard on a
branch production never takes, an ownership check the production loader
ignored, and a deletion service calling RPCs that did not exist.

So this file provisions the real thing through ``live_catalog_harness``: a
``CatalogDaemon`` over a real Unix socket, a ``SearchDaemon`` beside it,
content-addressed objects on disk, and an environment shaped the way a
self-hosted Runtime Host is shaped -- no ``TESTING``, no ``AUTH_DISABLED``, a
file-backed ``DATABASE_URL`` whose live sibling the daemon owns. Requests go
through ``api_app``; every authorization decision below runs unmocked, over
RPC, against real SQL. The harness documents the two seams it sets rather than
mocks, and sets nothing else. Each test states the guard it pins; deleting that
guard fails the test.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from tests_lite.live_catalog_harness import DEFAULT_INSTANCE_ID
from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import provision_live_catalog
from zerg.auth import managed_session_tokens as managed_tokens
from zerg.services import data_deletion
from zerg.services.data_deletion import SessionNotFound
from zerg.services.data_deletion import delete_session_data

INSTANCE_A = DEFAULT_INSTANCE_ID
INSTANCE_B = "instance-b"


@pytest.fixture()
def live():
    """A Runtime Host shaped the way production shapes one."""

    with provision_live_catalog(instance_id=INSTANCE_A) as catalog:
        yield catalog


@pytest.fixture()
def client(live: LiveCatalog):
    """``api_app`` with its import-time db dependencies forced to production's."""

    with live.http_client() as test_client:
        yield test_client


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
    cookie = live.browser_cookie(owner_id=owner, email="owner@contract.test")

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

    catalog_client = live.catalog_client()
    search_client = live.search_client()
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
