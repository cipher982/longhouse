from __future__ import annotations

import json
import logging
import re

import httpx

from control_plane.config import settings

logger = logging.getLogger(__name__)

PUBSUB_API_BASE = "https://pubsub.googleapis.com/v1"
PUBSUB_SCOPE = "https://www.googleapis.com/auth/pubsub"
SUBSCRIPTION_PREFIX = "gmail-push"
TOPIC_PATTERN = re.compile(r"^projects/(?P<project>[^/]+)/topics/(?P<topic>[^/]+)$")


class HostedGmailPubSubError(RuntimeError):
    """Raised when hosted Gmail Pub/Sub provisioning cannot complete."""


def _topic_project(topic_name: str) -> str:
    match = TOPIC_PATTERN.match(topic_name.strip())
    if not match:
        raise HostedGmailPubSubError(
            "CONTROL_PLANE_INSTANCE_GMAIL_PUBSUB_TOPIC must look like projects/<project>/topics/<topic>."
        )
    return str(match.group("project"))


def _subscription_name(subdomain: str) -> str:
    topic_name = settings.instance_gmail_pubsub_topic
    if not topic_name:
        raise HostedGmailPubSubError("Hosted Gmail Pub/Sub topic is not configured on the control plane.")

    project = _topic_project(topic_name)
    return f"projects/{project}/subscriptions/{SUBSCRIPTION_PREFIX}-{subdomain}"


def _push_endpoint(subdomain: str) -> str:
    return f"https://{subdomain}.{settings.root_domain}/api/email/webhook/google/pubsub"


def _push_audience(subdomain: str) -> str:
    return f"https://{subdomain}.{settings.root_domain}"


def _desired_push_config(subdomain: str) -> dict[str, object]:
    service_account_email = settings.instance_pubsub_sa_email
    if not service_account_email:
        raise HostedGmailPubSubError(
            "Hosted Gmail Pub/Sub push service account is not configured on the control plane."
        )

    return {
        "pushEndpoint": _push_endpoint(subdomain),
        "oidcToken": {
            "serviceAccountEmail": service_account_email,
            "audience": _push_audience(subdomain),
        },
    }


def _google_access_token() -> str:
    try:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import service_account
    except Exception as exc:  # noqa: BLE001
        raise HostedGmailPubSubError("google-auth is required for hosted Gmail Pub/Sub provisioning.") from exc

    credentials_json = settings.google_cloud_credentials_json

    try:
        if credentials_json:
            credentials = service_account.Credentials.from_service_account_info(
                json.loads(credentials_json),
                scopes=[PUBSUB_SCOPE],
            )
        else:
            credentials, _ = google.auth.default(scopes=[PUBSUB_SCOPE])
    except DefaultCredentialsError as exc:
        raise HostedGmailPubSubError(
            "Google Cloud credentials are not configured for hosted Gmail Pub/Sub provisioning.",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HostedGmailPubSubError(
            "Could not load Google Cloud credentials for hosted Gmail Pub/Sub provisioning."
        ) from exc

    try:
        credentials.refresh(GoogleAuthRequest())
    except Exception as exc:  # noqa: BLE001
        raise HostedGmailPubSubError(
            "Could not refresh Google Cloud credentials for hosted Gmail Pub/Sub provisioning."
        ) from exc

    token = getattr(credentials, "token", None)
    if not token:
        raise HostedGmailPubSubError(
            "Google Cloud credentials did not return an access token for Pub/Sub provisioning."
        )
    return str(token)


def _pubsub_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_google_access_token()}",
        "Content-Type": "application/json",
    }


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return response.text[:300] or f"HTTP {response.status_code}"

    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return response.text[:300] or f"HTTP {response.status_code}"


def _subscription_matches(
    existing: dict[str, object], desired_topic: str, desired_push_config: dict[str, object]
) -> bool:
    if existing.get("topic") != desired_topic:
        return False

    push_config = existing.get("pushConfig")
    if not isinstance(push_config, dict):
        return False

    existing_oidc = push_config.get("oidcToken")
    desired_oidc = desired_push_config.get("oidcToken")
    if not isinstance(existing_oidc, dict) or not isinstance(desired_oidc, dict):
        return False

    return (
        push_config.get("pushEndpoint") == desired_push_config.get("pushEndpoint")
        and existing_oidc.get("serviceAccountEmail") == desired_oidc.get("serviceAccountEmail")
        and existing_oidc.get("audience") == desired_oidc.get("audience")
    )


def ensure_instance_gmail_subscription(*, subdomain: str) -> str:
    """Create or reconcile the hosted Gmail Pub/Sub push subscription for an instance."""

    topic_name = settings.instance_gmail_pubsub_topic
    if not topic_name:
        raise HostedGmailPubSubError("Hosted Gmail Pub/Sub topic is not configured on the control plane.")

    subscription_name = _subscription_name(subdomain)
    desired_push_config = _desired_push_config(subdomain)
    headers = _pubsub_headers()
    subscription_url = f"{PUBSUB_API_BASE}/{subscription_name}"

    response = httpx.get(subscription_url, headers=headers, timeout=20.0)
    if response.status_code == 404:
        create_response = httpx.put(
            subscription_url,
            headers=headers,
            json={
                "topic": topic_name,
                "pushConfig": desired_push_config,
            },
            timeout=20.0,
        )
        if create_response.status_code == 409:
            response = httpx.get(subscription_url, headers=headers, timeout=20.0)
        else:
            if create_response.is_error:
                raise HostedGmailPubSubError(
                    f"Could not create hosted Gmail Pub/Sub subscription: {_error_message(create_response)}",
                )
            logger.info("Created hosted Gmail Pub/Sub subscription %s", subscription_name)
            return subscription_name
        if response.is_error:
            raise HostedGmailPubSubError(
                f"Could not inspect hosted Gmail Pub/Sub subscription after concurrent create: {_error_message(response)}",
            )

    if response.is_error:
        raise HostedGmailPubSubError(
            f"Could not inspect hosted Gmail Pub/Sub subscription: {_error_message(response)}",
        )

    subscription = response.json()
    if not isinstance(subscription, dict):
        raise HostedGmailPubSubError("Pub/Sub returned an invalid subscription response.")

    existing_topic = subscription.get("topic")
    if existing_topic != topic_name:
        raise HostedGmailPubSubError(
            f"Existing hosted Gmail Pub/Sub subscription points at {existing_topic!r}, expected {topic_name!r}.",
        )

    if _subscription_matches(subscription, topic_name, desired_push_config):
        return subscription_name

    modify_response = httpx.post(
        f"{subscription_url}:modifyPushConfig",
        headers=headers,
        json={"pushConfig": desired_push_config},
        timeout=20.0,
    )
    if modify_response.is_error:
        raise HostedGmailPubSubError(
            f"Could not update hosted Gmail Pub/Sub subscription: {_error_message(modify_response)}",
        )

    logger.info("Updated hosted Gmail Pub/Sub subscription %s", subscription_name)
    return subscription_name
