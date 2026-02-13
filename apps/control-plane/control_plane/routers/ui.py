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
*,*::before,*::after { box-sizing: border-box; }
body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       margin: 0; color: #fafafa; background: #030305; line-height: 1.6; }
.container { max-width: 560px; margin: 0 auto; padding: 3rem 1.5rem; }
h1 { font-size: 1.75rem; font-weight: 700; margin: 0 0 0.5rem 0; letter-spacing: -0.02em; }
h2 { font-size: 1.25rem; font-weight: 600; margin: 0 0 1rem 0; color: #fafafa; }
p { color: #b4b4bc; margin: 0.5rem 0; }
a { color: #818cf8; }
a:hover { color: #a5b4fc; }
strong { color: #fafafa; }
.card { background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
        border-radius: 16px; padding: 1.75rem; margin-bottom: 1.25rem;
        backdrop-filter: blur(8px); }
.card:hover { border-color: rgba(255,255,255,0.12); }
.btn { display: inline-block; padding: 0.65rem 1.5rem; border-radius: 8px; text-decoration: none;
       font-weight: 500; cursor: pointer; border: none; font-size: 0.95rem;
       transition: all 0.15s ease; line-height: 1.4; }
.btn-primary { background: #6366f1; color: #fff; border: 1px solid rgba(129,140,248,0.5); }
.btn-primary:hover { background: #4f46e5; box-shadow: 0 0 24px rgba(99,102,241,0.4); }
.btn-secondary { background: rgba(255,255,255,0.07); color: #fafafa;
                 border: 1px solid rgba(255,255,255,0.1); }
.btn-secondary:hover { background: rgba(255,255,255,0.1); border-color: rgba(129,140,248,0.4); }
.btn-danger { background: rgba(239,68,68,0.15); color: #fca5a5;
              border: 1px solid rgba(239,68,68,0.3); }
.btn-danger:hover { background: rgba(239,68,68,0.25); }
label { display: block; margin-top: 0.75rem; font-size: 0.875rem; color: #b4b4bc; font-weight: 500; }
input { width: 100%; padding: 0.6rem 0.75rem; margin-top: 0.35rem; border: 1px solid rgba(255,255,255,0.1);
        border-radius: 8px; box-sizing: border-box; background: rgba(255,255,255,0.05);
        color: #fafafa; font-size: 0.95rem; outline: none; transition: border-color 0.15s; }
input:focus { border-color: #6366f1; box-shadow: 0 0 0 2px rgba(99,102,241,0.2); }
button { margin-top: 1rem; }
table { border-collapse: collapse; width: 100%; margin-top: 0.75rem; }
th { text-align: left; padding: 0.6rem; border-bottom: 1px solid rgba(255,255,255,0.1);
     font-size: 0.8rem; color: #9898a3; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
td { text-align: left; padding: 0.6rem; border-bottom: 1px solid rgba(255,255,255,0.05);
     font-size: 0.9rem; color: #b4b4bc; }
small { color: #9898a3; }
.status-active { color: #22c55e; font-weight: 600; }
.status-provisioning { color: #f59e0b; font-weight: 600; }
.status-canceled { color: #ef4444; font-weight: 600; }
.status-failed { color: #ef4444; font-weight: 600; }
.spinner { display: inline-block; width: 28px; height: 28px; border: 3px solid rgba(255,255,255,0.1);
           border-top-color: #6366f1; border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.nav { background: rgba(3,3,5,0.85); backdrop-filter: blur(12px);
       border-bottom: 1px solid rgba(255,255,255,0.06); padding: 0.75rem 2rem;
       display: flex; justify-content: space-between; align-items: center; }
.nav a { color: #fafafa; text-decoration: none; font-weight: 500; font-size: 0.95rem; }
.nav a:hover { color: #818cf8; }
.subtitle { color: #9898a3; font-size: 0.95rem; margin-bottom: 2rem; }
.hero-center { text-align: center; padding: 4rem 0 2rem; }
.hero-center h1 { font-size: 2rem; }
.instance-url { display: block; font-family: 'JetBrains Mono', 'Fira Code', monospace;
                font-size: 0.95rem; color: #818cf8; margin: 0.25rem 0; }
.meta-row { display: flex; gap: 2rem; margin: 1rem 0; }
.meta-item { display: flex; flex-direction: column; gap: 0.15rem; }
.meta-label { font-size: 0.75rem; color: #9898a3; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
.meta-value { font-size: 0.95rem; color: #fafafa; }
.actions { display: flex; gap: 0.75rem; margin-top: 1.5rem; }
.google-btn { display: inline-flex; align-items: center; gap: 0.75rem; padding: 0.75rem 1.75rem; }
.google-btn svg { flex-shrink: 0; }
"""


_GOOGLE_ICON = '<svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2a10.3 10.3 0 0 0-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92a8.78 8.78 0 0 0 2.68-6.62z"/><path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.8.54-1.83.86-3.04.86-2.34 0-4.32-1.58-5.03-3.71H.96v2.33A9 9 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.97 10.71A5.41 5.41 0 0 1 3.69 9c0-.6.1-1.17.28-1.71V4.96H.96A9 9 0 0 0 0 9c0 1.45.35 2.82.96 4.04l3.01-2.33z"/><path fill="#EA4335" d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.59A9 9 0 0 0 9 0 9 9 0 0 0 .96 4.96l3.01 2.33C4.68 5.16 6.66 3.58 9 3.58z"/></svg>'


def _page(title: str, body: str, *, nav: bool = True) -> str:
    nav_html = ""
    if nav:
        nav_html = f"""
    <div class="nav">
      <a href="/dashboard"><strong>Longhouse</strong></a>
      <div style="display:flex;gap:1.25rem;align-items:center;">
        <a href="/dashboard">Dashboard</a>
        <a href="#" onclick="fetch('/auth/logout',{{method:'POST'}}).then(()=>location.href='/')" style="color:#9898a3;">Logout</a>
      </div>
    </div>"""

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title} - Longhouse</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
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
def home(request: Request, error: str | None = None, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)

    error_html = ""
    if error:
        error_html = f'''<div style="background:rgba(239,68,68,0.15);border:1px solid rgba(239,68,68,0.3);border-radius:8px;padding:0.75rem;margin-bottom:1rem;color:#fca5a5;font-size:0.9rem;">{html.escape(error)}</div>'''

    body = f"""
    <div class="hero-center">
      <h1>Longhouse</h1>
      <p class="subtitle">Sign in to manage your hosted instance.</p>
    </div>
    <div class="card" style="max-width:400px;margin:0 auto 1.25rem;">
      {error_html}
      <form method="post" action="/auth/login">
        <label>Email <input type="email" name="email" required placeholder="you@example.com"></label>
        <label>Password <input type="password" name="password" required minlength="8" placeholder="&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;"></label>
        <button type="submit" class="btn btn-primary" style="width:100%;text-align:center;">Sign In</button>
      </form>
      <p style="text-align:center;margin-top:0.75rem;font-size:0.875rem;color:#9898a3;">
        Don\'t have an account? <a href="/signup">Create one</a>
      </p>
    </div>
    <div style="max-width:400px;margin:0 auto;">
      <div style="display:flex;align-items:center;gap:1rem;margin:1rem 0;">
        <div style="flex:1;height:1px;background:rgba(255,255,255,0.1);"></div>
        <span style="color:#9898a3;font-size:0.8rem;text-transform:uppercase;letter-spacing:0.05em;">or</span>
        <div style="flex:1;height:1px;background:rgba(255,255,255,0.1);"></div>
      </div>
      <a href="/auth/google" class="btn btn-secondary google-btn" style="width:100%;text-align:center;justify-content:center;">{_GOOGLE_ICON} Continue with Google</a>
      <p style="text-align:center;margin-top:2rem;"><a href="https://longhouse.ai" style="color:#9898a3;font-size:0.875rem;">&larr; Back to longhouse.ai</a></p>
    </div>
    """
    return _page("Home", body, nav=False)


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request, error: str | None = None, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)

    error_html = ""
    if error:
        error_html = f'''<div style="background:rgba(239,68,68,0.15);border:1px solid rgba(239,68,68,0.3);border-radius:8px;padding:0.75rem;margin-bottom:1rem;color:#fca5a5;font-size:0.9rem;">{html.escape(error)}</div>'''

    body = f"""
    <div class="hero-center">
      <h1>Create Account</h1>
      <p class="subtitle">Get started with Longhouse.</p>
    </div>
    <div class="card" style="max-width:400px;margin:0 auto 1.25rem;">
      {error_html}
      <form method="post" action="/auth/signup">
        <label>Email <input type="email" name="email" required placeholder="you@example.com"></label>
        <label>Password <input type="password" name="password" required minlength="8" placeholder="Min. 8 characters"></label>
        <label>Confirm password <input type="password" name="password_confirm" required minlength="8" placeholder="Repeat password"></label>
        <button type="submit" class="btn btn-primary" style="width:100%;text-align:center;">Create Account</button>
      </form>
      <p style="text-align:center;margin-top:0.75rem;font-size:0.875rem;color:#9898a3;">
        Already have an account? <a href="/">Sign in</a>
      </p>
    </div>
    <div style="max-width:400px;margin:0 auto;">
      <div style="display:flex;align-items:center;gap:1rem;margin:1rem 0;">
        <div style="flex:1;height:1px;background:rgba(255,255,255,0.1);"></div>
        <span style="color:#9898a3;font-size:0.8rem;text-transform:uppercase;letter-spacing:0.05em;">or</span>
        <div style="flex:1;height:1px;background:rgba(255,255,255,0.1);"></div>
      </div>
      <a href="/auth/google" class="btn btn-secondary google-btn" style="width:100%;text-align:center;justify-content:center;">{_GOOGLE_ICON} Continue with Google</a>
      <p style="text-align:center;margin-top:2rem;"><a href="https://longhouse.ai" style="color:#9898a3;font-size:0.875rem;">&larr; Back to longhouse.ai</a></p>
    </div>
    """
    return _page("Sign Up", body, nav=False)


# ---------------------------------------------------------------------------
# Authenticated pages
# ---------------------------------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/", status_code=302)

    instance = db.query(Instance).filter(Instance.user_id == user.id).first()

    if instance and instance.status not in ("deprovisioned", "failed"):
        # Has instance — show it
        instance_url = f"https://{instance.subdomain}.{settings.root_domain}"
        status_class = f"status-{instance.status}" if instance.status in ("active", "provisioning", "canceled") else ""

        billing_btn = (
            '<a href="/billing/portal-redirect" class="btn btn-secondary">Manage Billing</a>'
            if user.stripe_customer_id else ""
        )

        body = f"""
        <h1>Your Instance</h1>
        <div class="card">
          <a href="{instance_url}" target="_blank" class="instance-url">{instance.subdomain}.{settings.root_domain}</a>
          <div class="meta-row">
            <div class="meta-item">
              <span class="meta-label">Status</span>
              <span class="meta-value {status_class}">{html.escape(instance.status)}</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">Plan</span>
              <span class="meta-value">{html.escape(user.subscription_status or 'free')}</span>
            </div>
          </div>
          <div class="actions">
            <a href="/dashboard/open-instance" class="btn btn-primary">Open Instance</a>
            {billing_btn}
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
        <p class="subtitle">Launch your own always-on Longhouse instance.</p>
        <div class="card">
          <h2>Hosted &mdash; $5/mo</h2>
          <p>Always-on instance, automatic updates, access from any device.</p>
          <form method="post" action="/dashboard/checkout">
            <button type="submit" class="btn btn-primary" style="margin-top:1rem;">Subscribe &amp; Launch Instance</button>
          </form>
        </div>
        <p style="text-align:center;margin-top:1.5rem;">
          <a href="https://longhouse.ai" style="color:#9898a3;font-size:0.875rem;">Or self-host free forever &rarr;</a>
        </p>
        """

    return _page("Dashboard", body)


@router.get("/dashboard/open-instance", response_class=HTMLResponse)
def open_instance(request: Request, db: Session = Depends(get_db)):
    """Issue a login token and redirect user to their instance with auto-auth."""
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/", status_code=302)

    instance = db.query(Instance).filter(Instance.user_id == user.id).first()
    if not instance:
        return RedirectResponse("/dashboard", status_code=302)

    instance_url = f"https://{instance.subdomain}.{settings.root_domain}"

    # Issue a short-lived JWT signed with the instance JWT secret
    import time

    from control_plane.routers.instances import _encode_jwt

    token = _encode_jwt(
        {"sub": str(user.id), "email": user.email, "exp": int(time.time()) + 300},
        settings.instance_jwt_secret,
    )

    # Redirect to instance SSO endpoint — sets cookie and redirects to /timeline
    sso_url = f"{instance_url}/api/auth/sso?token={token}"
    return RedirectResponse(sso_url, status_code=302)


@router.post("/dashboard/checkout")
def dashboard_checkout(request: Request, db: Session = Depends(get_db)):
    """Trigger Stripe checkout from dashboard — redirect to Stripe."""
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/", status_code=302)

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
        return RedirectResponse("/", status_code=302)

    instance = db.query(Instance).filter(Instance.user_id == user.id).first()

    if not instance:
        # Not provisioned yet — webhook might still be processing
        body = """
        <div class="card" style="text-align: center; padding: 3rem;">
          <div class="spinner"></div>
          <h2 style="margin-top: 1.25rem;">Setting up your instance</h2>
          <p>Waiting for payment confirmation...</p>
          <p><small>This page refreshes automatically.</small></p>
        </div>
        <script>setTimeout(() => location.reload(), 5000);</script>
        """
        return _page("Provisioning", body)

    if instance.status == "active":
        # Already ready — redirect to instance
        instance_url = f"https://{instance.subdomain}.{settings.root_domain}"
        return RedirectResponse(instance_url, status_code=302)

    if instance.status == "failed":
        body = """
        <div class="card" style="text-align: center; padding: 3rem;">
          <h2>Something went wrong</h2>
          <p>Your instance failed to provision. We've been notified.</p>
          <div class="actions" style="justify-content:center;margin-top:1.5rem;">
            <a href="mailto:hello@longhouse.ai?subject=Provisioning%20failure" class="btn btn-primary">Contact Support</a>
            <a href="/dashboard" class="btn btn-secondary">Back</a>
          </div>
        </div>
        """
        return _page("Provisioning Failed", body)

    # Provisioning in progress — poll health
    instance_url = f"https://{instance.subdomain}.{settings.root_domain}"
    health_url = f"{instance_url}/api/health"

    body = f"""
    <div class="card" style="text-align: center; padding: 3rem;">
      <div class="spinner" id="spinner"></div>
      <h2 style="margin-top: 1.25rem;" id="status-text">Starting your instance</h2>
      <p><code style="color:#818cf8;font-size:0.9rem;">{instance.subdomain}.{settings.root_domain}</code></p>
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
