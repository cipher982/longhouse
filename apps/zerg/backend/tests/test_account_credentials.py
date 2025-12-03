import json
from unittest.mock import patch

from zerg.connectors.registry import ConnectorType
from zerg.connectors.resolver import CredentialResolver
from zerg.models.models import AccountConnectorCredential
from zerg.models.models import ConnectorCredential
from zerg.utils.crypto import encrypt


def test_credential_resolver_fallback(db_session, test_user, sample_agent):
    """Test that CredentialResolver correctly prioritizes agent overrides over account credentials."""
    connector_type = ConnectorType.SLACK.value
    
    # 1. Setup: Create account-level credential
    account_creds = {"webhook_url": "https://account-level.com"}
    account_cred = AccountConnectorCredential(
        owner_id=test_user.id,
        connector_type=connector_type,
        encrypted_value=encrypt(json.dumps(account_creds)),
        display_name="Account Slack"
    )
    db_session.add(account_cred)
    db_session.commit()

    # Initialize resolver with owner_id
    resolver = CredentialResolver(agent_id=sample_agent.id, db=db_session, owner_id=test_user.id)

    # Case 1: Only account credential exists -> return account credential
    result = resolver.get(connector_type)
    assert result == account_creds
    assert resolver.get_resolution_source(connector_type) == "account"
    assert resolver.has(connector_type) is True

    # 2. Setup: Create agent-level override
    agent_creds = {"webhook_url": "https://agent-override.com"}
    agent_cred = ConnectorCredential(
        agent_id=sample_agent.id,
        connector_type=connector_type,
        encrypted_value=encrypt(json.dumps(agent_creds)),
        display_name="Agent Slack"
    )
    db_session.add(agent_cred)
    db_session.commit()

    # Clear cache to force re-resolution
    resolver.clear_cache()

    # Case 2: Both exist -> return agent override
    result = resolver.get(connector_type)
    assert result == agent_creds
    assert resolver.get_resolution_source(connector_type) == "agent"
    assert resolver.has(connector_type) is True

    # 3. Cleanup: Remove account credential
    db_session.delete(account_cred)
    db_session.commit()
    resolver.clear_cache()

    # Case 3: Only agent override exists -> return agent override
    result = resolver.get(connector_type)
    assert result == agent_creds
    assert resolver.get_resolution_source(connector_type) == "agent"

    # 4. Cleanup: Remove agent override
    db_session.delete(agent_cred)
    db_session.commit()
    resolver.clear_cache()

    # Case 4: Neither exists -> return None
    result = resolver.get(connector_type)
    assert result is None
    assert resolver.get_resolution_source(connector_type) == "none"
    assert resolver.has(connector_type) is False


def test_credential_resolver_no_owner(db_session, sample_agent):
    """Test resolver behavior when owner_id is not provided (legacy behavior)."""
    connector_type = ConnectorType.SLACK.value
    resolver = CredentialResolver(agent_id=sample_agent.id, db=db_session, owner_id=None)

    # Should return 'none' even if account creds exist (because we didn't pass owner_id)
    assert resolver.get(connector_type) is None
    assert resolver.get_resolution_source(connector_type) == "none"


def test_account_connectors_api_lifecycle(client, db_session, test_user):
    """Test the full lifecycle of account-level connectors via API."""
    
    # 1. List connectors - initially empty configuration
    response = client.get("/api/account/connectors")
    assert response.status_code == 200
    data = response.json()
    slack_status = next(c for c in data if c["type"] == "slack")
    assert slack_status["configured"] is False
    assert slack_status["test_status"] == "untested"

    # 2. Configure a connector
    payload = {
        "connector_type": "slack",
        "credentials": {"webhook_url": "https://hooks.slack.com/services/XXX/YYY/ZZZ"},
        "display_name": "My Company Slack"
    }
    response = client.post("/api/account/connectors", json=payload)
    assert response.status_code == 201
    assert response.json()["success"] is True

    # Verify database state
    cred = db_session.query(AccountConnectorCredential).filter_by(owner_id=test_user.id, connector_type="slack").first()
    assert cred is not None
    assert cred.display_name == "My Company Slack"

    # 3. List again - should be configured
    response = client.get("/api/account/connectors")
    data = response.json()
    slack_status = next(c for c in data if c["type"] == "slack")
    assert slack_status["configured"] is True
    assert slack_status["display_name"] == "My Company Slack"

    # 4. Test the configured connector
    # Mock the actual test execution to avoid network calls
    with patch("zerg.routers.account_connectors.test_connector") as mock_test:
        mock_test.return_value = {
            "success": True,
            "message": "Connected to Slack",
            "metadata": {"workspace": "TestWorkspace"}
        }
        
        response = client.post("/api/account/connectors/slack/test")
        assert response.status_code == 200
        assert response.json()["success"] is True
        assert response.json()["metadata"]["workspace"] == "TestWorkspace"

        # Verify DB update
        db_session.refresh(cred)
        assert cred.test_status == "success"
        assert cred.connector_metadata == {"workspace": "TestWorkspace"}

    # 5. Test "before save" endpoint
    with patch("zerg.routers.account_connectors.test_connector") as mock_test:
        mock_test.return_value = {"success": False, "message": "Invalid token"}
        
        test_payload = {
            "connector_type": "slack",
            "credentials": {"webhook_url": "bad_url"}
        }
        response = client.post("/api/account/connectors/test", json=test_payload)
        assert response.status_code == 200
        assert response.json()["success"] is False

    # 6. Delete connector
    response = client.delete("/api/account/connectors/slack")
    assert response.status_code == 204

    # Verify DB deletion
    cred = db_session.query(AccountConnectorCredential).filter_by(owner_id=test_user.id, connector_type="slack").first()
    assert cred is None

    # 7. List again - should be unconfigured
    response = client.get("/api/account/connectors")
    data = response.json()
    slack_status = next(c for c in data if c["type"] == "slack")
    assert slack_status["configured"] is False

