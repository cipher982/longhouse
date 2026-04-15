"""Stripe billing: checkout session creation and billing portal."""
from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from control_plane.config import settings
from control_plane.db import get_db
from control_plane.models import User
from control_plane.routers.auth import get_current_user

router = APIRouter(prefix="/billing", tags=["billing"])
logger = logging.getLogger(__name__)
CHECKOUT_SUBDOMAIN_METADATA_KEY = "requested_subdomain"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_stripe():
    """Fail fast if Stripe is not configured."""
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Stripe not configured")


def _get_stripe():
    """Return configured stripe module."""
    _require_stripe()
    import stripe

    stripe.api_key = settings.stripe_secret_key
    return stripe


def _checkout_metadata(user: User) -> dict[str, str]:
    metadata = {"longhouse_user_id": str(user.id)}
    if user.pending_subdomain:
        metadata[CHECKOUT_SUBDOMAIN_METADATA_KEY] = user.pending_subdomain
    return metadata


def _create_checkout_session(user: User, db: Session, *, cancel_url: str):
    # Already subscribed -> point to portal
    if user.subscription_status == "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Already subscribed. Use /billing/portal to manage.",
        )

    stripe = _get_stripe()

    if not settings.stripe_price_id:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="STRIPE_PRICE_ID not configured")

    # Create or reuse Stripe customer
    if not user.stripe_customer_id:
        customer = stripe.Customer.create(
            email=user.email,
            metadata={"longhouse_user_id": str(user.id)},
        )
        user.stripe_customer_id = customer.id
        db.commit()

    session = stripe.checkout.Session.create(
        customer=user.stripe_customer_id,
        mode="subscription",
        line_items=[{"price": settings.stripe_price_id, "quantity": 1}],
        success_url=f"https://control.{settings.root_domain}/provisioning?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=cancel_url,
        client_reference_id=str(user.id),
        metadata=_checkout_metadata(user),
    )

    logger.info("Created checkout session %s for %s (subdomain=%s)", session.id, user.email, user.pending_subdomain)
    return session


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/checkout")
def create_checkout(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Create a Stripe Checkout session for a new subscription.

    Requires authenticated session with verified email.
    """
    if not user.email_verified:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Email not verified")

    session = _create_checkout_session(user, db, cancel_url=f"https://{settings.root_domain}")
    return {"checkout_url": session.url, "session_id": session.id}


@router.post("/portal")
def create_portal(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Create a Stripe billing portal session for subscription management."""
    stripe = _get_stripe()

    if not user.stripe_customer_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No billing account")

    portal = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=f"https://control.{settings.root_domain}/dashboard",
    )

    return {"portal_url": portal.url}
