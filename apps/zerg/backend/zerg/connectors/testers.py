"""Connector testers for validating credentials before saving.

Each connector has a tester function that makes a real API call to verify
the credentials are valid. Test results include success status, message,
and optional metadata (e.g., username, scopes discovered during test).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from zerg.connectors.registry import ConnectorType

logger = logging.getLogger(__name__)

# Timeout for test requests (seconds)
TEST_TIMEOUT = 10.0


def test_connector(connector_type: ConnectorType | str, credentials: dict[str, Any]) -> dict[str, Any]:
    """Test a connector credential by making a real API call.

    Args:
        connector_type: ConnectorType enum or string value
        credentials: Dict of credential fields to test

    Returns:
        {
            "success": bool,
            "message": str,
            "metadata": optional dict with discovered info
        }
    """
    # Convert string to enum if needed
    if isinstance(connector_type, str):
        try:
            connector_type = ConnectorType(connector_type)
        except ValueError:
            return {"success": False, "message": f"Unknown connector type: {connector_type}"}

    testers = {
        ConnectorType.SLACK: _test_slack,
        ConnectorType.DISCORD: _test_discord,
        ConnectorType.EMAIL: _test_email,
        ConnectorType.SMS: _test_sms,
        ConnectorType.GITHUB: _test_github,
        ConnectorType.JIRA: _test_jira,
        ConnectorType.LINEAR: _test_linear,
        ConnectorType.NOTION: _test_notion,
        ConnectorType.IMESSAGE: _test_imessage,
        ConnectorType.SSH: _test_ssh,
    }

    tester = testers.get(connector_type)
    if not tester:
        return {"success": False, "message": f"No tester implemented for {connector_type.value}"}

    try:
        return tester(credentials)
    except httpx.TimeoutException:
        return {"success": False, "message": "Connection timed out"}
    except httpx.ConnectError:
        return {"success": False, "message": "Failed to connect to service"}
    except Exception as e:
        logger.exception("Connector test failed for %s", connector_type.value)
        return {"success": False, "message": f"Test failed: {str(e)}"}


def _test_slack(creds: dict[str, Any]) -> dict[str, Any]:
    """Send a test message to Slack webhook.

    Note: Slack webhooks don't have a "dry run" mode, so we send an actual
    test message. The message is clearly marked as a test.
    """
    webhook_url = creds.get("webhook_url")
    if not webhook_url:
        return {"success": False, "message": "Missing webhook_url"}

    if not webhook_url.startswith("https://hooks.slack.com/"):
        return {"success": False, "message": "Invalid Slack webhook URL format"}

    response = httpx.post(
        webhook_url,
        json={"text": ":wrench: Zerg test message - your Slack webhook is working!"},
        timeout=TEST_TIMEOUT,
    )

    if response.status_code == 200 and response.text == "ok":
        return {"success": True, "message": "Test message sent to Slack"}
    return {"success": False, "message": f"Slack returned {response.status_code}: {response.text}"}


def _test_discord(creds: dict[str, Any]) -> dict[str, Any]:
    """Send a test message to Discord webhook.

    Note: Discord webhooks don't have a "dry run" mode, so we send an actual
    test message. The message is clearly marked as a test.
    """
    webhook_url = creds.get("webhook_url")
    if not webhook_url:
        return {"success": False, "message": "Missing webhook_url"}

    if not webhook_url.startswith("https://discord.com/api/webhooks/"):
        return {"success": False, "message": "Invalid Discord webhook URL format"}

    response = httpx.post(
        webhook_url,
        json={"content": ":wrench: Zerg test message - your Discord webhook is working!"},
        timeout=TEST_TIMEOUT,
    )

    # Discord returns 204 No Content on success
    if response.status_code in (200, 204):
        return {"success": True, "message": "Test message sent to Discord"}
    return {"success": False, "message": f"Discord returned {response.status_code}: {response.text}"}


def _test_email(creds: dict[str, Any]) -> dict[str, Any]:
    """Validate Resend API key by listing domains.

    We don't send an actual email during test - just verify the API key
    is valid and discover available domains.
    """
    api_key = creds.get("api_key")
    from_email = creds.get("from_email")

    if not api_key:
        return {"success": False, "message": "Missing api_key"}
    if not from_email:
        return {"success": False, "message": "Missing from_email"}

    response = httpx.get(
        "https://api.resend.com/domains",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=TEST_TIMEOUT,
    )

    if response.status_code == 200:
        try:
            data = response.json()
            domains = data.get("data", [])
            domain_names = [d.get("name") for d in domains if d.get("name")]
            return {
                "success": True,
                "message": f"API key valid. Domains: {', '.join(domain_names) if domain_names else 'none'}",
                "metadata": {"domains": domain_names, "from_email": from_email},
            }
        except Exception:
            return {"success": True, "message": "API key valid"}

    if response.status_code == 401:
        return {"success": False, "message": "Invalid API key"}
    return {"success": False, "message": f"Resend returned {response.status_code}"}


def _test_sms(creds: dict[str, Any]) -> dict[str, Any]:
    """Validate Twilio credentials by fetching account info.

    We don't send an actual SMS during test - just verify the credentials
    are valid and discover account info.
    """
    account_sid = creds.get("account_sid")
    auth_token = creds.get("auth_token")
    from_number = creds.get("from_number")

    if not account_sid:
        return {"success": False, "message": "Missing account_sid"}
    if not auth_token:
        return {"success": False, "message": "Missing auth_token"}
    if not from_number:
        return {"success": False, "message": "Missing from_number"}

    response = httpx.get(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}.json",
        auth=(account_sid, auth_token),
        timeout=TEST_TIMEOUT,
    )

    if response.status_code == 200:
        try:
            data = response.json()
            return {
                "success": True,
                "message": f"Connected to Twilio account: {data.get('friendly_name', account_sid)}",
                "metadata": {
                    "friendly_name": data.get("friendly_name"),
                    "from_number": from_number,
                },
            }
        except Exception:
            return {"success": True, "message": "Twilio credentials valid"}

    if response.status_code == 401:
        return {"success": False, "message": "Invalid Account SID or Auth Token"}
    return {"success": False, "message": f"Twilio returned {response.status_code}"}


def _test_github(creds: dict[str, Any]) -> dict[str, Any]:
    """Validate GitHub token by fetching authenticated user info.

    Discovers username and available scopes from the API response.
    """
    token = creds.get("token")
    if not token:
        return {"success": False, "message": "Missing token"}

    response = httpx.get(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=TEST_TIMEOUT,
    )

    if response.status_code == 200:
        try:
            data = response.json()
            # Get scopes from response header
            scopes = response.headers.get("X-OAuth-Scopes", "")
            scope_list = [s.strip() for s in scopes.split(",") if s.strip()]

            return {
                "success": True,
                "message": f"Connected as {data.get('login')}",
                "metadata": {
                    "login": data.get("login"),
                    "name": data.get("name"),
                    "scopes": scope_list,
                },
            }
        except Exception:
            return {"success": True, "message": "GitHub token valid"}

    if response.status_code == 401:
        return {"success": False, "message": "Invalid or expired token"}
    if response.status_code == 403:
        return {"success": False, "message": "Token lacks required permissions"}
    return {"success": False, "message": f"GitHub returned {response.status_code}"}


def _test_jira(creds: dict[str, Any]) -> dict[str, Any]:
    """Validate Jira credentials by fetching current user info."""
    domain = creds.get("domain")
    email = creds.get("email")
    api_token = creds.get("api_token")

    if not domain:
        return {"success": False, "message": "Missing domain"}
    if not email:
        return {"success": False, "message": "Missing email"}
    if not api_token:
        return {"success": False, "message": "Missing api_token"}

    # Normalize domain format
    domain = domain.strip()
    if domain.startswith("https://"):
        domain = domain[8:]
    if domain.startswith("http://"):
        domain = domain[7:]
    if not domain.endswith(".atlassian.net"):
        domain = f"{domain}.atlassian.net"

    response = httpx.get(
        f"https://{domain}/rest/api/3/myself",
        auth=(email, api_token),
        timeout=TEST_TIMEOUT,
    )

    if response.status_code == 200:
        try:
            data = response.json()
            return {
                "success": True,
                "message": f"Connected as {data.get('displayName', email)}",
                "metadata": {
                    "displayName": data.get("displayName"),
                    "emailAddress": data.get("emailAddress"),
                    "domain": domain,
                },
            }
        except Exception:
            return {"success": True, "message": "Jira credentials valid"}

    if response.status_code == 401:
        return {"success": False, "message": "Invalid email or API token"}
    if response.status_code == 403:
        return {"success": False, "message": "Access forbidden - check permissions"}
    return {"success": False, "message": f"Jira returned {response.status_code}"}


def _test_linear(creds: dict[str, Any]) -> dict[str, Any]:
    """Validate Linear API key by fetching viewer info via GraphQL."""
    api_key = creds.get("api_key")
    if not api_key:
        return {"success": False, "message": "Missing api_key"}

    response = httpx.post(
        "https://api.linear.app/graphql",
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
        },
        json={"query": "{ viewer { id name email } }"},
        timeout=TEST_TIMEOUT,
    )

    if response.status_code == 200:
        try:
            data = response.json()
            if "errors" in data:
                error_msg = data["errors"][0].get("message", "Unknown error")
                return {"success": False, "message": f"Linear API error: {error_msg}"}

            viewer = data.get("data", {}).get("viewer", {})
            if viewer:
                return {
                    "success": True,
                    "message": f"Connected as {viewer.get('name', 'Unknown')}",
                    "metadata": {
                        "name": viewer.get("name"),
                        "email": viewer.get("email"),
                    },
                }
            return {"success": False, "message": "Could not fetch viewer info"}
        except Exception:
            return {"success": True, "message": "Linear API key valid"}

    if response.status_code == 401:
        return {"success": False, "message": "Invalid API key"}
    return {"success": False, "message": f"Linear returned {response.status_code}"}


def _test_notion(creds: dict[str, Any]) -> dict[str, Any]:
    """Validate Notion integration token by fetching bot user info."""
    api_key = creds.get("api_key")
    if not api_key:
        return {"success": False, "message": "Missing api_key"}

    response = httpx.get(
        "https://api.notion.com/v1/users/me",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": "2022-06-28",
        },
        timeout=TEST_TIMEOUT,
    )

    if response.status_code == 200:
        try:
            data = response.json()
            bot_name = data.get("name") or data.get("bot", {}).get("owner", {}).get("workspace", {}).get("name")
            return {
                "success": True,
                "message": f"Connected as {bot_name or 'Integration'}",
                "metadata": {
                    "name": data.get("name"),
                    "type": data.get("type"),
                },
            }
        except Exception:
            return {"success": True, "message": "Notion token valid"}

    if response.status_code == 401:
        return {"success": False, "message": "Invalid integration token"}
    return {"success": False, "message": f"Notion returned {response.status_code}"}


def _test_imessage(creds: dict[str, Any]) -> dict[str, Any]:
    """Validate iMessage configuration.

    iMessage requires the agent to run on a macOS host with Messages.app
    configured. We can only verify the configuration is set - actual
    sending capability depends on the runtime environment.
    """
    enabled = creds.get("enabled")
    if not enabled or str(enabled).lower() not in ("true", "1", "yes"):
        return {"success": False, "message": "iMessage not enabled"}

    return {
        "success": True,
        "message": "iMessage configured (requires macOS host at runtime)",
        "metadata": {"enabled": True},
    }


def _test_ssh(creds: dict[str, Any]) -> dict[str, Any]:
    """Validate SSH target configuration.

    SSH connectivity can only be tested from a runner that has SSH access
    to the target. This tester validates the configuration format and
    returns success if the config looks valid.

    Actual SSH connectivity testing requires:
    1. An online runner with SSH access to the target
    2. Using runner_ssh_exec to test the connection

    The frontend can optionally trigger a live test by dispatching a simple
    command (e.g., 'echo ok') via runner_ssh_exec after the config is saved.
    """
    name = creds.get("name")
    host = creds.get("host")
    ssh_config_name = creds.get("ssh_config_name")

    if not name:
        return {"success": False, "message": "Missing target name"}

    # Either host or ssh_config_name must be provided
    if not host and not ssh_config_name:
        return {"success": False, "message": "Missing host or ssh_config_name"}

    # Validate host format if provided
    if host:
        # Basic host validation - not empty, no whitespace, no obvious issues
        host = host.strip()
        if not host or " " in host:
            return {"success": False, "message": "Invalid host format"}

    # Validate port if provided
    port = creds.get("port")
    if port:
        try:
            port_num = int(port)
            if port_num < 1 or port_num > 65535:
                return {"success": False, "message": "Port must be between 1 and 65535"}
        except ValueError:
            return {"success": False, "message": "Port must be a number"}

    # Build metadata
    metadata: dict[str, Any] = {"name": name}
    if host:
        metadata["host"] = host
    if ssh_config_name:
        metadata["ssh_config_name"] = ssh_config_name
    if creds.get("user"):
        metadata["user"] = creds["user"]
    if port:
        metadata["port"] = int(port)

    return {
        "success": True,
        "message": f"SSH target '{name}' configured (test connectivity via runner)",
        "metadata": metadata,
    }
