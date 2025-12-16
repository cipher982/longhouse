"""Tests for Runners API endpoints.

Tests enrollment, registration, and management of runners.
"""

import threading
from datetime import datetime
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.crud import runner_crud
from zerg.models.models import User


def test_create_enroll_token(client: TestClient, db_session: Session, test_user: User):
    """Test creating an enrollment token."""
    response = client.post("/api/runners/enroll-token", headers={"X-User-ID": str(test_user.id)})
    assert response.status_code == 200

    data = response.json()
    assert "enroll_token" in data
    assert "expires_at" in data
    assert "swarmlet_url" in data
    assert "docker_command" in data

    # Token should be a long string
    assert len(data["enroll_token"]) > 30

    # Docker command should contain the token
    assert data["enroll_token"] in data["docker_command"]


def test_register_runner_with_valid_token(client: TestClient, db_session: Session, test_user: User):
    """Test registering a runner with a valid enrollment token."""
    # Create enrollment token
    token_record, plaintext_token = runner_crud.create_enroll_token(
        db=db_session,
        owner_id=test_user.id,
        ttl_minutes=10,
    )

    # Register runner
    response = client.post(
        "/api/runners/register",
        json={
            "enroll_token": plaintext_token,
            "name": "test-runner",
            "labels": {"env": "test"},
            "metadata": {"hostname": "test-host"},
        },
    )
    assert response.status_code == 200

    data = response.json()
    assert "runner_id" in data
    assert "runner_secret" in data
    assert data["name"] == "test-runner"
    assert len(data["runner_secret"]) > 30

    # Verify runner was created in database
    runner = runner_crud.get_runner(db_session, data["runner_id"])
    assert runner is not None
    assert runner.owner_id == test_user.id
    assert runner.name == "test-runner"
    assert runner.labels == {"env": "test"}
    assert runner.runner_metadata == {"hostname": "test-host"}
    assert runner.status == "offline"

    # Verify token was consumed
    token = runner_crud.get_enroll_token_by_hash(db_session, token_record.token_hash)
    assert token.used_at is not None


def test_register_runner_auto_name(client: TestClient, db_session: Session, test_user: User):
    """Test registering a runner without providing a name."""
    token_record, plaintext_token = runner_crud.create_enroll_token(
        db=db_session,
        owner_id=test_user.id,
        ttl_minutes=10,
    )

    response = client.post(
        "/api/runners/register",
        json={"enroll_token": plaintext_token},
    )
    assert response.status_code == 200

    data = response.json()
    # Auto-generated name should start with "runner-" and have random suffix
    assert data["name"].startswith("runner-")
    assert len(data["name"]) > len("runner-")  # Has suffix


def test_register_runner_with_expired_token(client: TestClient, db_session: Session, test_user: User):
    """Test registering a runner with an expired token fails."""
    # Create token that's already expired
    token = runner_crud.generate_token()
    token_hash = runner_crud.hash_token(token)

    from zerg.models.models import RunnerEnrollToken
    from zerg.utils.time import utc_now_naive

    db_token = RunnerEnrollToken(
        owner_id=test_user.id,
        token_hash=token_hash,
        expires_at=utc_now_naive() - timedelta(minutes=1),  # Already expired
    )
    db_session.add(db_token)
    db_session.commit()

    response = client.post(
        "/api/runners/register",
        json={"enroll_token": token, "name": "test-runner"},
    )
    assert response.status_code == 400
    assert "Invalid or expired" in response.json()["detail"]


def test_register_runner_with_duplicate_name(client: TestClient, db_session: Session, test_user: User):
    """Test registering a runner with a duplicate name fails."""
    # Create first runner
    token1, plaintext1 = runner_crud.create_enroll_token(db_session, test_user.id)
    response1 = client.post(
        "/api/runners/register",
        json={"enroll_token": plaintext1, "name": "my-runner"},
    )
    assert response1.status_code == 200

    # Try to create second runner with same name
    token2, plaintext2 = runner_crud.create_enroll_token(db_session, test_user.id)
    response2 = client.post(
        "/api/runners/register",
        json={"enroll_token": plaintext2, "name": "my-runner"},
    )
    assert response2.status_code == 409
    assert "already exists" in response2.json()["detail"]


def test_list_runners_empty(client: TestClient, db_session: Session, test_user: User):
    """Test listing runners when none exist."""
    response = client.get("/api/runners/", headers={"X-User-ID": str(test_user.id)})
    assert response.status_code == 200

    data = response.json()
    assert data["runners"] == []


def test_list_runners_with_data(client: TestClient, db_session: Session, test_user: User):
    """Test listing runners returns user's runners."""
    # Create a runner
    secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db_session,
        owner_id=test_user.id,
        name="test-runner",
        auth_secret=secret,
        labels={"env": "test"},
        capabilities=["exec.readonly"],
    )

    response = client.get("/api/runners/", headers={"X-User-ID": str(test_user.id)})
    assert response.status_code == 200

    data = response.json()
    assert len(data["runners"]) == 1
    assert data["runners"][0]["id"] == runner.id
    assert data["runners"][0]["name"] == "test-runner"
    assert data["runners"][0]["labels"] == {"env": "test"}
    assert data["runners"][0]["capabilities"] == ["exec.readonly"]
    assert data["runners"][0]["status"] == "offline"


def test_list_runners_only_own(client: TestClient, db_session: Session, test_user: User):
    """Test that users only see their own runners."""
    # Create another user and runner
    other_user = crud.create_user(db_session, email="other@example.com")
    secret1 = runner_crud.generate_token()
    runner_crud.create_runner(
        db=db_session,
        owner_id=other_user.id,
        name="other-runner",
        auth_secret=secret1,
    )

    # Create runner for test user
    secret2 = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db_session,
        owner_id=test_user.id,
        name="my-runner",
        auth_secret=secret2,
    )

    response = client.get("/api/runners/", headers={"X-User-ID": str(test_user.id)})
    assert response.status_code == 200

    data = response.json()
    assert len(data["runners"]) == 1
    assert data["runners"][0]["id"] == runner.id
    assert data["runners"][0]["owner_id"] == test_user.id


def test_get_runner_success(client: TestClient, db_session: Session, test_user: User):
    """Test getting a specific runner."""
    secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db_session,
        owner_id=test_user.id,
        name="test-runner",
        auth_secret=secret,
    )

    response = client.get(f"/api/runners/{runner.id}", headers={"X-User-ID": str(test_user.id)})
    assert response.status_code == 200

    data = response.json()
    assert data["id"] == runner.id
    assert data["name"] == "test-runner"


def test_get_runner_not_found(client: TestClient, db_session: Session, test_user: User):
    """Test getting non-existent runner returns 404."""
    response = client.get("/api/runners/9999", headers={"X-User-ID": str(test_user.id)})
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_get_runner_not_owner(client: TestClient, db_session: Session, test_user: User):
    """Test users cannot view other users' runners."""
    other_user = crud.create_user(db_session, email="other@example.com")
    secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db_session,
        owner_id=other_user.id,
        name="other-runner",
        auth_secret=secret,
    )

    response = client.get(f"/api/runners/{runner.id}", headers={"X-User-ID": str(test_user.id)})
    assert response.status_code == 404


def test_update_runner_name(client: TestClient, db_session: Session, test_user: User):
    """Test updating a runner's name."""
    secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db_session,
        owner_id=test_user.id,
        name="old-name",
        auth_secret=secret,
    )

    response = client.patch(
        f"/api/runners/{runner.id}",
        json={"name": "new-name"},
        headers={"X-User-ID": str(test_user.id)},
    )
    assert response.status_code == 200

    data = response.json()
    assert data["name"] == "new-name"

    # Verify in database
    updated_runner = runner_crud.get_runner(db_session, runner.id)
    assert updated_runner.name == "new-name"


def test_update_runner_labels(client: TestClient, db_session: Session, test_user: User):
    """Test updating a runner's labels."""
    secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db_session,
        owner_id=test_user.id,
        name="test-runner",
        auth_secret=secret,
        labels={"env": "dev"},
    )

    response = client.patch(
        f"/api/runners/{runner.id}",
        json={"labels": {"env": "prod", "region": "us-east"}},
        headers={"X-User-ID": str(test_user.id)},
    )
    assert response.status_code == 200

    data = response.json()
    assert data["labels"] == {"env": "prod", "region": "us-east"}


def test_update_runner_capabilities(client: TestClient, db_session: Session, test_user: User):
    """Test updating a runner's capabilities."""
    secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db_session,
        owner_id=test_user.id,
        name="test-runner",
        auth_secret=secret,
        capabilities=["exec.readonly"],
    )

    response = client.patch(
        f"/api/runners/{runner.id}",
        json={"capabilities": ["exec.full", "docker"]},
        headers={"X-User-ID": str(test_user.id)},
    )
    assert response.status_code == 200

    data = response.json()
    assert data["capabilities"] == ["exec.full", "docker"]


def test_update_runner_duplicate_name(client: TestClient, db_session: Session, test_user: User):
    """Test updating a runner to a duplicate name fails."""
    secret1 = runner_crud.generate_token()
    runner1 = runner_crud.create_runner(
        db=db_session,
        owner_id=test_user.id,
        name="runner-1",
        auth_secret=secret1,
    )

    secret2 = runner_crud.generate_token()
    runner2 = runner_crud.create_runner(
        db=db_session,
        owner_id=test_user.id,
        name="runner-2",
        auth_secret=secret2,
    )

    response = client.patch(
        f"/api/runners/{runner2.id}",
        json={"name": "runner-1"},
        headers={"X-User-ID": str(test_user.id)},
    )
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_update_runner_not_owner(client: TestClient, db_session: Session, test_user: User):
    """Test users cannot update other users' runners."""
    other_user = crud.create_user(db_session, email="other@example.com")
    secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db_session,
        owner_id=other_user.id,
        name="other-runner",
        auth_secret=secret,
    )

    response = client.patch(
        f"/api/runners/{runner.id}",
        json={"name": "hacked-name"},
        headers={"X-User-ID": str(test_user.id)},
    )
    assert response.status_code == 404


def test_revoke_runner_success(client: TestClient, db_session: Session, test_user: User):
    """Test revoking a runner."""
    secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db_session,
        owner_id=test_user.id,
        name="test-runner",
        auth_secret=secret,
    )

    response = client.post(f"/api/runners/{runner.id}/revoke", headers={"X-User-ID": str(test_user.id)})
    assert response.status_code == 200

    data = response.json()
    assert data["success"] is True
    assert "revoked" in data["message"]

    # Verify status in database
    revoked_runner = runner_crud.get_runner(db_session, runner.id)
    assert revoked_runner.status == "revoked"


def test_revoke_runner_not_found(client: TestClient, db_session: Session, test_user: User):
    """Test revoking non-existent runner returns 404."""
    response = client.post("/api/runners/9999/revoke", headers={"X-User-ID": str(test_user.id)})
    assert response.status_code == 404


def test_revoke_runner_not_owner(client: TestClient, db_session: Session, test_user: User):
    """Test users cannot revoke other users' runners."""
    other_user = crud.create_user(db_session, email="other@example.com")
    secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db_session,
        owner_id=other_user.id,
        name="other-runner",
        auth_secret=secret,
    )

    response = client.post(f"/api/runners/{runner.id}/revoke", headers={"X-User-ID": str(test_user.id)})
    assert response.status_code == 404


def test_token_reuse_prevented(client: TestClient, db_session: Session, test_user: User):
    """Test that enrollment tokens cannot be reused."""
    token_record, plaintext_token = runner_crud.create_enroll_token(
        db=db_session,
        owner_id=test_user.id,
        ttl_minutes=10,
    )

    # Register first runner
    response1 = client.post(
        "/api/runners/register",
        json={"enroll_token": plaintext_token, "name": "runner-1"},
    )
    assert response1.status_code == 200

    # Try to reuse token
    response2 = client.post(
        "/api/runners/register",
        json={"enroll_token": plaintext_token, "name": "runner-2"},
    )
    assert response2.status_code == 400
    assert "Invalid or expired" in response2.json()["detail"]


def test_concurrent_token_consumption(client: TestClient, db_session: Session, test_user: User):
    """Test that concurrent registration attempts with the same token only succeed once.

    This verifies the atomic UPDATE...RETURNING implementation prevents race conditions.
    """
    token_record, plaintext_token = runner_crud.create_enroll_token(
        db=db_session,
        owner_id=test_user.id,
        ttl_minutes=10,
    )

    results = []
    errors = []

    def register_runner(name: str):
        """Thread worker to attempt registration."""
        try:
            response = client.post(
                "/api/runners/register",
                json={"enroll_token": plaintext_token, "name": name},
            )
            results.append((name, response.status_code, response.json()))
        except Exception as e:
            errors.append((name, str(e)))

    # Launch 5 concurrent registration attempts
    threads = []
    for i in range(5):
        t = threading.Thread(target=register_runner, args=(f"runner-{i}",))
        threads.append(t)
        t.start()

    # Wait for all threads
    for t in threads:
        t.join()

    # Verify no exceptions occurred
    assert len(errors) == 0, f"Unexpected errors: {errors}"

    # Verify exactly one success (200) and four failures (400)
    success_count = sum(1 for _, status, _ in results if status == 200)
    failure_count = sum(1 for _, status, _ in results if status == 400)

    assert success_count == 1, f"Expected exactly 1 success, got {success_count}"
    assert failure_count == 4, f"Expected 4 failures, got {failure_count}"

    # Verify the failure responses have correct error message
    for name, status, data in results:
        if status == 400:
            assert "Invalid or expired" in data["detail"]


# ---------------------------------------------------------------------------
# Rotate Secret Tests
# ---------------------------------------------------------------------------


def test_rotate_secret_success(client: TestClient, db_session: Session, test_user: User):
    """Test rotating a runner's secret returns a new secret."""
    # Create a runner
    original_secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db_session,
        owner_id=test_user.id,
        name="test-runner",
        auth_secret=original_secret,
    )
    original_hash = runner.auth_secret_hash

    # Rotate secret
    response = client.post(
        f"/api/runners/{runner.id}/rotate-secret",
        headers={"X-User-ID": str(test_user.id)},
    )
    assert response.status_code == 200

    data = response.json()
    assert data["runner_id"] == runner.id
    assert "runner_secret" in data
    assert len(data["runner_secret"]) > 30  # Should be a long token
    assert "rotated" in data["message"].lower()

    # Verify the secret hash changed in database
    db_session.refresh(runner)
    assert runner.auth_secret_hash != original_hash

    # Verify the new secret is different from the original
    assert data["runner_secret"] != original_secret

    # Verify the new secret hashes to the new stored hash
    new_secret_hash = runner_crud.hash_token(data["runner_secret"])
    assert new_secret_hash == runner.auth_secret_hash


def test_rotate_secret_invalidates_old_secret(client: TestClient, db_session: Session, test_user: User):
    """Test that the old secret becomes invalid after rotation."""
    # Create a runner
    original_secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db_session,
        owner_id=test_user.id,
        name="test-runner",
        auth_secret=original_secret,
    )

    # Verify original secret validates
    original_hash = runner_crud.hash_token(original_secret)
    assert original_hash == runner.auth_secret_hash

    # Rotate secret
    response = client.post(
        f"/api/runners/{runner.id}/rotate-secret",
        headers={"X-User-ID": str(test_user.id)},
    )
    assert response.status_code == 200
    new_secret = response.json()["runner_secret"]

    # Verify old secret no longer validates
    db_session.refresh(runner)
    assert runner_crud.hash_token(original_secret) != runner.auth_secret_hash

    # Verify new secret validates
    assert runner_crud.hash_token(new_secret) == runner.auth_secret_hash


def test_rotate_secret_not_found(client: TestClient, db_session: Session, test_user: User):
    """Test rotating secret for non-existent runner returns 404."""
    response = client.post(
        "/api/runners/9999/rotate-secret",
        headers={"X-User-ID": str(test_user.id)},
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_rotate_secret_not_owner(client: TestClient, db_session: Session, test_user: User):
    """Test users cannot rotate other users' runner secrets."""
    other_user = crud.create_user(db_session, email="other@example.com")
    secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db_session,
        owner_id=other_user.id,
        name="other-runner",
        auth_secret=secret,
    )

    response = client.post(
        f"/api/runners/{runner.id}/rotate-secret",
        headers={"X-User-ID": str(test_user.id)},
    )
    assert response.status_code == 404


def test_rotate_secret_revoked_runner(client: TestClient, db_session: Session, test_user: User):
    """Test rotating secret for a revoked runner fails."""
    secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db_session,
        owner_id=test_user.id,
        name="test-runner",
        auth_secret=secret,
    )

    # Revoke the runner
    runner_crud.revoke_runner(db_session, runner.id)

    response = client.post(
        f"/api/runners/{runner.id}/rotate-secret",
        headers={"X-User-ID": str(test_user.id)},
    )
    assert response.status_code == 400
    assert "revoked" in response.json()["detail"].lower()


def test_rotate_secret_marks_runner_offline(client: TestClient, db_session: Session, test_user: User):
    """Test that rotating secret marks the runner as offline."""
    secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db_session,
        owner_id=test_user.id,
        name="test-runner",
        auth_secret=secret,
    )

    # Manually set runner to online for test
    runner.status = "online"
    db_session.commit()

    # Rotate secret
    response = client.post(
        f"/api/runners/{runner.id}/rotate-secret",
        headers={"X-User-ID": str(test_user.id)},
    )
    assert response.status_code == 200

    # Verify runner is now offline
    db_session.refresh(runner)
    assert runner.status == "offline"
