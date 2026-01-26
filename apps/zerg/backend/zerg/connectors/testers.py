"""Connector testers for validating credentials before saving.

Each connector has a tester function that makes a real API call to verify
the credentials are valid. Test results include success status, message,
and optional metadata (e.g., username, scopes discovered during test).
"""

from __future__ import annotations

# Pytest collects modules matching "test*.py"; prevent accidental test discovery here.
__test__ = False

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
        ConnectorType.TRACCAR: _test_traccar,
        ConnectorType.WHOOP: _test_whoop,
        ConnectorType.OBSIDIAN: _test_obsidian,
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


# Prevent pytest from collecting this helper as a test function.
test_connector.__test__ = False


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
    """Validate AWS SES credentials by getting send quota.

    We don't send an actual email during test - just verify the credentials
    are valid and discover account info (send quota, verified identities).
    """
    import boto3
    from botocore.exceptions import ClientError

    access_key_id = creds.get("access_key_id")
    secret_access_key = creds.get("secret_access_key")
    region = creds.get("region", "us-east-1")
    from_email = creds.get("from_email")

    if not access_key_id:
        return {"success": False, "message": "Missing access_key_id"}
    if not secret_access_key:
        return {"success": False, "message": "Missing secret_access_key"}
    if not from_email:
        return {"success": False, "message": "Missing from_email"}

    try:
        client = boto3.client(
            "ses",
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

        # Get send quota to verify credentials
        quota = client.get_send_quota()
        max_24hr = quota.get("Max24HourSend", 0)
        sent_24hr = quota.get("SentLast24Hours", 0)

        # Get verified identities to show available senders
        identities = client.list_identities(IdentityType="EmailAddress", MaxItems=10)
        verified_emails = identities.get("Identities", [])

        return {
            "success": True,
            "message": f"SES connected. Quota: {int(sent_24hr)}/{int(max_24hr)} emails/24h",
            "metadata": {
                "max_24hr_send": max_24hr,
                "sent_24hr": sent_24hr,
                "verified_emails": verified_emails,
                "region": region,
                "from_email": from_email,
            },
        }

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))

        if error_code in ("InvalidClientTokenId", "SignatureDoesNotMatch"):
            return {"success": False, "message": "Invalid AWS credentials"}
        elif error_code == "AccessDenied":
            return {"success": False, "message": "Access denied - check IAM permissions for SES"}
        return {"success": False, "message": f"SES error: {error_message}"}


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

    iMessage requires the fiche to run on a macOS host with Messages.app
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


def _test_traccar(creds: dict[str, Any]) -> dict[str, Any]:
    """Validate Traccar credentials by fetching session info.

    Authenticates with the Traccar server and retrieves the current session
    to verify credentials are valid and discover user information.
    """
    url = creds.get("url")
    username = creds.get("username")
    password = creds.get("password")
    device_id = creds.get("device_id")

    if not url:
        return {"success": False, "message": "Missing url"}
    if not username:
        return {"success": False, "message": "Missing username"}
    if not password:
        return {"success": False, "message": "Missing password"}

    # Normalize URL
    url = url.rstrip("/")

    # Authenticate and get session info
    response = httpx.post(
        f"{url}/api/session",
        data={"email": username, "password": password},
        timeout=TEST_TIMEOUT,
    )

    if response.status_code == 200:
        try:
            data = response.json()
            user_name = data.get("name") or data.get("email", username)

            metadata: dict[str, Any] = {
                "user": user_name,
                "server": url,
            }

            # If device_id provided, verify it exists
            if device_id:
                cookies = response.cookies
                devices_response = httpx.get(
                    f"{url}/api/devices",
                    cookies=cookies,
                    timeout=TEST_TIMEOUT,
                )
                if devices_response.status_code == 200:
                    devices = devices_response.json()
                    device = next((d for d in devices if str(d.get("id")) == str(device_id)), None)
                    if device:
                        metadata["device"] = device.get("name", f"Device {device_id}")
                    else:
                        return {"success": False, "message": f"Device ID {device_id} not found"}

            return {
                "success": True,
                "message": f"Connected as {user_name}",
                "metadata": metadata,
            }
        except Exception:
            return {"success": True, "message": "Traccar credentials valid"}

    if response.status_code == 401:
        return {"success": False, "message": "Invalid username or password"}
    return {"success": False, "message": f"Traccar returned {response.status_code}"}


def _test_whoop(creds: dict[str, Any]) -> dict[str, Any]:
    """Validate WHOOP credentials by fetching user profile.

    Uses the access token to fetch user profile info. If refresh_token is
    provided, we could refresh the token, but for a simple test we just
    verify the current token works.
    """
    access_token = creds.get("access_token")

    if not access_token:
        return {"success": False, "message": "Missing access_token"}

    response = httpx.get(
        "https://api.prod.whoop.com/developer/v1/user/profile/basic",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=TEST_TIMEOUT,
    )

    if response.status_code == 200:
        try:
            data = response.json()
            first_name = data.get("first_name", "")
            last_name = data.get("last_name", "")
            name = f"{first_name} {last_name}".strip() or "WHOOP User"

            return {
                "success": True,
                "message": f"Connected as {name}",
                "metadata": {
                    "user_id": data.get("user_id"),
                    "name": name,
                },
            }
        except Exception:
            return {"success": True, "message": "WHOOP token valid"}

    if response.status_code == 401:
        return {"success": False, "message": "Invalid or expired access token"}
    if response.status_code == 403:
        return {"success": False, "message": "Token lacks required scopes"}
    return {"success": False, "message": f"WHOOP returned {response.status_code}"}


def _test_obsidian(creds: dict[str, Any]) -> dict[str, Any]:
    """Validate Obsidian configuration.

    Obsidian access happens via a Runner that has filesystem access to the vault.
    We can only verify the configuration is set - actual access depends on the
    Runner being online and having the vault path accessible.
    """
    vault_path = creds.get("vault_path")
    runner_name = creds.get("runner_name")

    if not vault_path:
        return {"success": False, "message": "Missing vault_path"}
    if not runner_name:
        return {"success": False, "message": "Missing runner_name"}

    return {
        "success": True,
        "message": f"Configured for runner '{runner_name}'",
        "metadata": {
            "vault_path": vault_path,
            "runner_name": runner_name,
        },
    }
