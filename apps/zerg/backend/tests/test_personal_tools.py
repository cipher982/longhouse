"""Tests for personal tools (Traccar, WHOOP, Obsidian) - Phase 4 v2.1."""

import json

import pytest
from sqlalchemy.orm import Session

from zerg.models.models import AccountConnectorCredential, User
from zerg.utils.crypto import decrypt, encrypt


# ---------------------------------------------------------------------------
# Credential Seeding Tests
# ---------------------------------------------------------------------------


def test_seed_credentials_script_imports():
    """Verify seed script can be imported."""
    from scripts.seed_personal_credentials import main, load_credentials, find_user, seed_credential
    assert callable(main)
    assert callable(load_credentials)
    assert callable(find_user)
    assert callable(seed_credential)


def test_seed_credential_creates_new(db_session: Session, test_user: User):
    """Test seeding creates new credential."""
    from scripts.seed_personal_credentials import seed_credential

    creds = {"url": "https://test.com", "token": "abc123"}
    result = seed_credential(db_session, test_user, "traccar", creds, force=False)

    assert result is True

    # Commit (seed_credential doesn't commit - caller does)
    db_session.commit()

    # Verify credential was created
    credential = db_session.query(AccountConnectorCredential).filter(
        AccountConnectorCredential.owner_id == test_user.id,
        AccountConnectorCredential.connector_type == "traccar",
    ).first()

    assert credential is not None
    assert credential.display_name == "Personal Traccar"

    # Verify encryption works
    decrypted = json.loads(decrypt(credential.encrypted_value))
    assert decrypted["url"] == "https://test.com"
    assert decrypted["token"] == "abc123"


def test_seed_credential_skips_existing_without_force(db_session: Session, test_user: User):
    """Test seeding skips existing credential without --force."""
    from scripts.seed_personal_credentials import seed_credential

    # Create existing credential
    existing = AccountConnectorCredential(
        owner_id=test_user.id,
        connector_type="traccar",
        encrypted_value=encrypt(json.dumps({"url": "old"})),
    )
    db_session.add(existing)
    db_session.commit()

    creds = {"url": "https://new.com", "token": "xyz"}
    result = seed_credential(db_session, test_user, "traccar", creds, force=False)

    assert result is False  # Skipped


def test_seed_credential_overwrites_with_force(db_session: Session, test_user: User):
    """Test seeding overwrites existing credential with --force."""
    from scripts.seed_personal_credentials import seed_credential

    # Create existing credential
    existing = AccountConnectorCredential(
        owner_id=test_user.id,
        connector_type="traccar",
        encrypted_value=encrypt(json.dumps({"url": "old"})),
    )
    db_session.add(existing)
    db_session.commit()
    old_id = existing.id

    creds = {"url": "https://new.com", "token": "xyz"}
    result = seed_credential(db_session, test_user, "traccar", creds, force=True)

    assert result is True
    db_session.commit()

    # Verify credential was updated (same ID)
    credential = db_session.query(AccountConnectorCredential).filter(
        AccountConnectorCredential.id == old_id
    ).first()

    assert credential is not None
    # Decrypt and verify it was updated
    decrypted = json.loads(decrypt(credential.encrypted_value))
    assert decrypted["url"] == "https://new.com"


def test_seed_all_personal_connectors(db_session: Session, test_user: User):
    """Test seeding all three personal connectors."""
    from scripts.seed_personal_credentials import seed_credential, PERSONAL_CONNECTORS

    creds_map = {
        "traccar": {"url": "https://traccar.test", "token": "tk1"},
        "whoop": {"access_token": "whoop-token", "refresh_token": "refresh-tk"},
        "obsidian": {"vault_path": "/vault", "runner_name": "laptop"},
    }

    for connector_type in PERSONAL_CONNECTORS:
        result = seed_credential(db_session, test_user, connector_type, creds_map[connector_type])
        assert result is True

    db_session.commit()

    # Verify all were created
    count = db_session.query(AccountConnectorCredential).filter(
        AccountConnectorCredential.owner_id == test_user.id
    ).count()
    assert count == 3


def test_credential_encryption_roundtrip(db_session: Session, test_user: User):
    """Test that credentials are properly encrypted and decrypted."""
    from scripts.seed_personal_credentials import seed_credential

    original_creds = {
        "url": "https://secret-server.com",
        "token": "super-secret-token-12345",
        "device_id": "999",
    }

    seed_credential(db_session, test_user, "traccar", original_creds)
    db_session.commit()

    # Retrieve and decrypt
    credential = db_session.query(AccountConnectorCredential).filter(
        AccountConnectorCredential.owner_id == test_user.id,
        AccountConnectorCredential.connector_type == "traccar",
    ).first()

    assert credential is not None

    # Verify the encrypted value is not plaintext
    assert original_creds["token"] not in credential.encrypted_value

    # Decrypt and verify
    decrypted = json.loads(decrypt(credential.encrypted_value))
    assert decrypted == original_creds


def test_find_user_by_email(db_session: Session, test_user: User):
    """Test finding user by email in seed script."""
    from scripts.seed_personal_credentials import find_user

    found = find_user(db_session, test_user.email)
    assert found is not None
    assert found.id == test_user.id


def test_find_first_user(db_session: Session, test_user: User):
    """Test finding first user when no email specified."""
    from scripts.seed_personal_credentials import find_user

    found = find_user(db_session, None)
    assert found is not None
