"""Control plane UI pages: home, dashboard, provisioning status, admin."""
from __future__ import annotations

import html

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Form
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from control_plane.config import settings
from control_plane.db import get_db
from control_plane.models import Instance
from control_plane.models import User
from control_plane.routers.auth import SESSION_COOKIE_NAME
from control_plane.routers.auth import _decode_jwt
from control_plane.services.provisioner import Provisioner

router = APIRouter(tags=["ui"])


# ---------------------------------------------------------------------------
# Shared layout
# ---------------------------------------------------------------------------

_STYLES = """
body { font-family: ui-sans-serif, system-ui, -apple-system; margin: 0; color: #111; background: #fafafa; }
.container { max-width: 640px; margin: 0 auto; padding: 2rem; }
h1 { font-size: 1.5rem; margin-bottom: 1.5rem; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 1.5rem; margin-bottom: 1rem; }
.btn { display: inline-block; padding: 0.6rem 1.2rem; border-radius: 8px; text-decoration: none;
       font-weight: 500; cursor: pointer; border: none; font-size: 0.95rem; }
.btn-primary { background: #111; color: #fff; }
.btn-primary:hover { background: #333; }
.btn-secondary { background: #f3f4f6; color: #111; border: 1px solid #d1d5db; }
label { display: block; margin-top: 0.5rem; font-size: 0.9rem; color: #374151; }
input { width: 100%; padding: 0.5rem; margin-top: 0.25rem; border: 1px solid #d1d5db;
        border-radius: 6px; box-sizing: border-box; }
button { margin-top: 0.75rem; }
table { border-collapse: collapse; width: 100%; margin-top: 0.5rem; }
th, td { text-align: left; padding: 0.5rem; border-bottom: 1px solid #e5e7eb; font-size: 0.9rem; }
small { color: #6b7280; }
.status-active { color: #059669; font-weight: 600; }
.status-provisioning { color: #d97706; font-weight: 600; }
.status-canceled { color: #dc2626; font-weight: 600; }
.spinner { display: inline-block; width: 20px; height: 20px; border: 3px solid #e5e7eb;
           border-top-color: #111; border-radius: 50%; animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.nav { background: #fff; border-bottom: 1px solid #e5e7eb; padding: 0.75rem 2rem; display: flex;
       justify-content: space-between; align-items: center; }
.nav a { color: #111; text-decoration: none; font-weight: 500; }
"""


def _page(title: str, body: str, *, nav: bool = True) -> str:
    nav_html = ""
    if nav:
        nav_html = f"""
    <div class="nav">
      <a href="/dashboard"><strong>Longhouse</strong></a>
      <div>
        <a href="/dashboard" style="margin-right:1rem;">Dashboard</a>
        <a href="#" onclick="fetch('/auth/logout',{{method:'POST'}}).then(()=>location.href='/')">Logout</a>
      </div>
    </div>"""

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title} - Longhouse</title>
    <style>{_STYLES}</style>
  </head>
  <body>
    {nav_html}
    <div class="container">
      {body}
    </div>
  </body>
</html>"""


# ---------------------------------------------------------------------------
# Session helper (read-only — doesn't set cookies)
# ---------------------------------------------------------------------------


def _get_user_from_cookie(request: Request, db: Session) -> User | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    try:
        payload = _decode_jwt(token, settings.jwt_secret)
        return db.query(User).filter(User.id == int(payload["sub"])).first()
    except (ValueError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)

    body = """
    <h1>Longhouse Control Plane</h1>
    <div class="card">
      <p>Manage your hosted Longhouse instance.</p>
      <a href="/auth/google" class="btn btn-primary">Sign in with Google</a>
    </div>
    """
    return _page("Home", body, nav=False)


# ---------------------------------------------------------------------------
# Authenticated pages
# ---------------------------------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/auth/google", status_code=302)

    instance = db.query(Instance).filter(Instance.user_id == user.id).first()

    if instance and instance.status not in ("deprovisioned",):
        # Has instance — show it
        instance_url = f"https://{instance.subdomain}.{settings.root_domain}"
        status_class = f"status-{instance.status}" if instance.status in ("active", "provisioning", "canceled") else ""

        body = f"""
        <h1>Your Instance</h1>
        <div class="card">
          <p><strong>URL:</strong> <a href="{instance_url}" target="_blank">{instance_url}</a></p>
          <p><strong>Status:</strong> <span class="{status_class}">{instance.status}</span></p>
          <p><strong>Subscription:</strong> {user.subscription_status or 'none'}</p>
          <div style="margin-top: 1rem;">
            <a href="{instance_url}" class="btn btn-primary" target="_blank">Open Instance</a>
            <a href="/billing/portal-redirect" class="btn btn-secondary" style="margin-left: 0.5rem;">Manage Billing</a>
          </div>
        </div>
        """
    elif user.subscription_status == "active":
        # Paid but not yet provisioned — redirect to provisioning status
        return RedirectResponse("/provisioning", status_code=302)
    else:
        # No subscription — offer checkout
        body = """
        <h1>Get Started</h1>
        <div class="card">
          <p>You don't have an instance yet. Subscribe to get your own Longhouse instance.</p>
          <form method="post" action="/dashboard/checkout" style="margin-top: 1rem;">
            <button type="submit" class="btn btn-primary">Subscribe & Launch Instance</button>
          </form>
        </div>
        """

    return _page("Dashboard", body)


@router.post("/dashboard/checkout")
def dashboard_checkout(request: Request, db: Session = Depends(get_db)):
    """Trigger Stripe checkout from dashboard — redirect to Stripe."""
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/auth/google", status_code=302)

    # Delegate to the billing API
    from control_plane.routers.billing import _get_stripe

    stripe = _get_stripe()

    if not settings.stripe_price_id:
        return _page("Error", '<div class="card"><p>Billing not configured yet.</p></div>')

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
        cancel_url=f"https://control.{settings.root_domain}/dashboard",
        client_reference_id=str(user.id),
        metadata={"longhouse_user_id": str(user.id)},
    )

    return RedirectResponse(session.url, status_code=303)


@router.get("/billing/portal-redirect")
def billing_portal_redirect(request: Request, db: Session = Depends(get_db)):
    """Redirect authenticated user to Stripe billing portal."""
    user = _get_user_from_cookie(request, db)
    if not user or not user.stripe_customer_id:
        return RedirectResponse("/dashboard", status_code=302)

    from control_plane.routers.billing import _get_stripe

    stripe = _get_stripe()
    portal = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=f"https://control.{settings.root_domain}/dashboard",
    )
    return RedirectResponse(portal.url, status_code=303)


# ---------------------------------------------------------------------------
# Provisioning status page
# ---------------------------------------------------------------------------


@router.get("/provisioning", response_class=HTMLResponse)
def provisioning_status(request: Request, db: Session = Depends(get_db)):
    """Show provisioning progress. Polls /api/instances/{id} for health."""
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/auth/google", status_code=302)

    instance = db.query(Instance).filter(Instance.user_id == user.id).first()

    if not instance:
        # Not provisioned yet — webhook might still be processing
        body = """
        <h1>Setting up your instance...</h1>
        <div class="card" style="text-align: center; padding: 3rem;">
          <div class="spinner"></div>
          <p style="margin-top: 1rem;">Waiting for payment confirmation...</p>
          <p><small>This page will refresh automatically.</small></p>
        </div>
        <script>setTimeout(() => location.reload(), 5000);</script>
        """
        return _page("Provisioning", body)

    if instance.status == "active":
        # Already ready — redirect to instance
        instance_url = f"https://{instance.subdomain}.{settings.root_domain}"
        return RedirectResponse(instance_url, status_code=302)

    # Provisioning in progress — poll health
    instance_url = f"https://{instance.subdomain}.{settings.root_domain}"
    health_url = f"{instance_url}/api/health"

    body = f"""
    <h1>Your instance is starting up...</h1>
    <div class="card" style="text-align: center; padding: 3rem;">
      <div class="spinner" id="spinner"></div>
      <p style="margin-top: 1rem;" id="status-text">Container is starting...</p>
      <p><small>{instance.subdomain}.{settings.root_domain}</small></p>
    </div>
    <script>
      const healthUrl = "{health_url}";
      const instanceUrl = "{instance_url}";
      let attempts = 0;

      async function checkHealth() {{
        attempts++;
        try {{
          const resp = await fetch(healthUrl, {{ mode: 'no-cors' }});
          // no-cors means we can't read status, but if it doesn't throw, container is up
          document.getElementById('status-text').textContent = 'Instance is ready! Redirecting...';
          document.getElementById('spinner').style.display = 'none';
          setTimeout(() => window.location.href = instanceUrl, 1500);
          return;
        }} catch (e) {{
          // Still starting up
        }}

        if (attempts < 60) {{
          setTimeout(checkHealth, 3000);
        }} else {{
          document.getElementById('status-text').textContent = 'Taking longer than expected. Check back in a minute.';
          document.getElementById('spinner').style.display = 'none';
        }}
      }}

      setTimeout(checkHealth, 3000);
    </script>
    """
    return _page("Provisioning", body)


# ---------------------------------------------------------------------------
# Admin pages (existing, unchanged)
# ---------------------------------------------------------------------------


@router.get("/admin", response_class=HTMLResponse)
def admin(db: Session = Depends(get_db)):
    rows = db.query(Instance, User).join(User, Instance.user_id == User.id).all()
    table_rows = "".join(
        f"<tr><td>{inst.id}</td><td>{html.escape(user.email)}</td>"
        f"<td>{html.escape(inst.subdomain)}</td><td>{inst.status}</td></tr>"
        for inst, user in rows
    )
    if not table_rows:
        table_rows = '<tr><td colspan=4><em>No instances yet</em></td></tr>'

    body = f"""
    <h1>Provision Instance</h1>
    <div class="card">
      <form method="post" action="/admin/provision">
        <label>Admin token <input type="password" name="token" required></label>
        <label>Email <input type="email" name="email" required></label>
        <label>Subdomain <input type="text" name="subdomain" required></label>
        <button type="submit" class="btn btn-primary">Provision</button>
      </form>
      <small>Will create user + provision instance container.</small>
    </div>
    <div class="card">
      <h2>Instances</h2>
      <table>
        <thead><tr><th>ID</th><th>Email</th><th>Subdomain</th><th>Status</th></tr></thead>
        <tbody>{table_rows}</tbody>
      </table>
    </div>
    """
    return _page("Admin Provisioning", body)


@router.post("/admin/provision", response_class=HTMLResponse)
def admin_provision(
    token: str = Form(...),
    email: str = Form(...),
    subdomain: str = Form(...),
    db: Session = Depends(get_db),
):
    if token != settings.admin_token:
        raise HTTPException(status_code=403, detail="Invalid admin token")

    email = email.strip().lower()
    subdomain = subdomain.strip().lower()

    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(email=email)
        db.add(user)
        db.commit()
        db.refresh(user)

    existing = db.query(Instance).filter(Instance.user_id == user.id).first()
    if existing:
        body = (
            f'<p>Instance already exists for {html.escape(email)} '
            f'({html.escape(existing.subdomain)}).</p><p><a href="/admin">Back</a></p>'
        )
        return _page("Provisioning", body)

    provisioner = Provisioner()
    result = provisioner.provision_instance(subdomain, owner_email=email)

    instance = Instance(
        user_id=user.id,
        subdomain=subdomain,
        container_name=result.container_name,
        data_path=result.data_path,
        status="provisioning",
    )
    db.add(instance)
    db.commit()

    body = (
        f"<p>Provisioned <strong>{html.escape(subdomain)}</strong> for {html.escape(email)}.</p>"
        f"<p>Container: {html.escape(result.container_name)}</p>"
        f'<p><a href="/admin">Back</a></p>'
    )
    return _page("Provisioned", body)
