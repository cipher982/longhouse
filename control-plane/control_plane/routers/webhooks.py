"""Stripe webhook handler with signature verification and event processing."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from control_plane.config import settings
from control_plane.db import get_db
from control_plane.models import Instance
from control_plane.models import User

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


def _stripe_webhook_secrets() -> list[tuple[str, str]]:
    secrets: list[tuple[str, str]] = []
    if settings.stripe_webhook_secret:
        secrets.append(("live", settings.stripe_webhook_secret))
    if settings.stripe_test_webhook_secret:
        secrets.append(("test", settings.stripe_test_webhook_secret))
    return secrets


def _construct_stripe_event(payload: bytes, sig_header: str) -> tuple[str, dict[str, Any]]:
    import stripe

    had_signature_error = False
    for secret_kind, secret in _stripe_webhook_secrets():
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, secret)
            return secret_kind, event
        except stripe.error.SignatureVerificationError:
            had_signature_error = True
            continue
        except Exception as exc:
            logger.error("Stripe webhook error: %s", exc)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook parsing error") from exc

    if had_signature_error:
        logger.warning("Stripe webhook signature verification failed")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature")

    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Stripe not configured")


@router.post("/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Handle Stripe webhook events with signature verification.

    Events handled:
    - checkout.session.completed  -> set subscription active, trigger provisioning
    - customer.subscription.updated -> update subscription status
    - customer.subscription.deleted -> mark canceled, schedule deprovision
    - invoice.paid           -> recover from past_due
    - invoice.payment_failed -> mark past_due
    """
    if not settings.stripe_secret_key or not _stripe_webhook_secrets():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Stripe not configured")

    import stripe

    stripe.api_key = settings.stripe_secret_key

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if not sig_header:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing stripe-signature header")

    secret_kind, event = _construct_stripe_event(payload, sig_header)
    livemode = bool(event.get("livemode", True))
    if livemode != (secret_kind == "live"):
        logger.warning(
            "Stripe webhook livemode mismatch: event_livemode=%s secret_kind=%s id=%s",
            livemode,
            secret_kind,
            event.get("id", "unknown"),
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook mode mismatch")

    event_type = event["type"]
    data = event["data"]["object"]

    logger.info(
        "Stripe webhook: %s (id=%s, livemode=%s, secret_kind=%s)",
        event_type,
        event.get("id", "unknown"),
        livemode,
        secret_kind,
    )

    if not livemode and not settings.stripe_process_test_events:
        logger.info("Ignoring verified Stripe test webhook: %s (id=%s)", event_type, event.get("id", "unknown"))
        return {"ok": True, "ignored": "test_mode"}

    if event_type == "checkout.session.completed":
        _handle_checkout_completed(data, db)
    elif event_type == "customer.subscription.updated":
        _handle_subscription_updated(data, db)
    elif event_type == "customer.subscription.deleted":
        _handle_subscription_deleted(data, db)
    elif event_type == "invoice.paid":
        _handle_invoice_paid(data, db)
    elif event_type == "invoice.payment_failed":
        _handle_payment_failed(data, db)
    else:
        logger.debug(f"Unhandled Stripe event type: {event_type}")

    return {"ok": True}


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


def _resolve_user_from_event(data: dict, db: Session) -> User | None:
    """Find the user associated with a Stripe event."""
    # Try client_reference_id first (set during checkout)
    ref_id = data.get("client_reference_id")
    if ref_id:
        user = db.query(User).filter(User.id == int(ref_id)).first()
        if user:
            return user

    # Fall back to customer ID lookup
    customer_id = data.get("customer")
    if customer_id:
        user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
        if user:
            return user

    return None


def _handle_checkout_completed(data: dict, db: Session) -> None:
    """Handle successful checkout: activate subscription and provision instance."""
    user = _resolve_user_from_event(data, db)
    if not user:
        logger.error(f"checkout.session.completed: could not resolve user (ref={data.get('client_reference_id')})")
        return

    # Idempotency: skip if already active and provisioned
    if user.subscription_status == "active":
        existing = db.query(Instance).filter(Instance.user_id == user.id).first()
        if existing and existing.status != "deprovisioned":
            logger.info(f"Checkout completed but user {user.email} already active+provisioned, skipping")
            return

    # Update subscription info
    subscription_id = data.get("subscription")
    customer_id = data.get("customer")

    if customer_id and not user.stripe_customer_id:
        user.stripe_customer_id = customer_id
    user.subscription_status = "active"
    db.commit()

    logger.info(f"Subscription activated for {user.email} (sub={subscription_id})")

    # Trigger provisioning
    existing = db.query(Instance).filter(Instance.user_id == user.id).first()
    if existing and existing.status != "deprovisioned":
        logger.info(f"Instance already exists for {user.email} ({existing.subdomain}), skipping provision")
        return

    # Use user-chosen slug when available; fall back to email derivation
    from control_plane.routers.instances import _derive_subdomain_from_email
    from control_plane.routers.instances import _is_valid_subdomain

    subdomain = user.pending_subdomain
    # Check claimed state first (race guard), then validate format
    if subdomain and not db.query(Instance).filter(Instance.subdomain == subdomain).first() and _is_valid_subdomain(subdomain):
        pass  # use the chosen slug
    else:
        subdomain = _derive_subdomain_from_email(user.email, db)

    # Clear and commit before provisioning so the slug is released
    # regardless of whether provisioning succeeds or fails.
    user.pending_subdomain = None
    db.commit()

    try:
        from control_plane.services.provisioner import Provisioner

        provisioner = Provisioner()
        result = provisioner.provision_instance(subdomain, owner_email=user.email)

        instance = Instance(
            user_id=user.id,
            subdomain=subdomain,
            container_name=result.container_name,
            data_path=result.data_path,
            password_hash=result.password_hash,
            status="provisioning",
        )
        db.add(instance)
        db.commit()

        logger.info(f"Provisioned instance {subdomain} for {user.email}")
    except Exception:
        logger.exception(f"Failed to provision instance for {user.email}")
        # Record failure so dashboard shows error instead of infinite spinner
        failed = Instance(user_id=user.id, subdomain=subdomain, container_name="", data_path="", status="failed")
        db.add(failed)
        db.commit()


def _handle_subscription_updated(data: dict, db: Session) -> None:
    """Handle subscription status changes."""
    customer_id = data.get("customer")
    if not customer_id:
        return

    user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
    if not user:
        logger.warning(f"subscription.updated: unknown customer {customer_id}")
        return

    new_status = data.get("status", "unknown")  # active, past_due, unpaid, canceled, etc.
    old_status = user.subscription_status
    user.subscription_status = new_status
    db.commit()

    logger.info(f"Subscription updated for {user.email}: {old_status} -> {new_status}")


def _handle_subscription_deleted(data: dict, db: Session) -> None:
    """Handle subscription cancellation: mark canceled, graceful deprovision."""
    customer_id = data.get("customer")
    if not customer_id:
        return

    user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
    if not user:
        logger.warning(f"subscription.deleted: unknown customer {customer_id}")
        return

    user.subscription_status = "canceled"
    db.commit()

    logger.info(f"Subscription canceled for {user.email} -- instance preserved for grace period")
    # NOTE: Don't immediately deprovision. A background job should handle
    # grace period + data preservation + notification before actual deprovision.


def _handle_invoice_paid(data: dict, db: Session) -> None:
    """Handle successful invoice payment: recover from past_due if needed."""
    customer_id = data.get("customer")
    if not customer_id:
        return

    user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
    if not user:
        logger.warning(f"invoice.paid: unknown customer {customer_id}")
        return

    if user.subscription_status in ("past_due", "unpaid"):
        user.subscription_status = "active"
        db.commit()
        logger.info(f"Payment recovered for {user.email} -- restored to active")
    else:
        logger.debug(f"invoice.paid for {user.email} (status={user.subscription_status}), no action needed")


def _handle_payment_failed(data: dict, db: Session) -> None:
    """Handle failed payment: mark as past_due."""
    customer_id = data.get("customer")
    if not customer_id:
        return

    user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
    if not user:
        logger.warning(f"invoice.payment_failed: unknown customer {customer_id}")
        return

    user.subscription_status = "past_due"
    db.commit()

    logger.info(f"Payment failed for {user.email} -- marked past_due")
