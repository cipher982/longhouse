"""Control plane UI pages: home, dashboard, provisioning status, admin."""
from __future__ import annotations

import hashlib
import hmac
import html
from pathlib import Path
import re as _re
import time
import urllib.parse

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Form
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from control_plane.config import settings
from control_plane.db import get_db
from control_plane.models import Instance
from control_plane.models import User
from control_plane.routers.auth import SESSION_COOKIE_NAME
from control_plane.routers.auth import _append_return_to
from control_plane.routers.auth import _decode_jwt
from control_plane.routers.auth import _issue_instance_sso_token
from control_plane.routers.instances import _build_migration_status
from control_plane.services.provisioner import Provisioner
from longhouse_shared.redirects import normalize_local_return_to

router = APIRouter(tags=["ui"])


# ---------------------------------------------------------------------------
# Shared layout
# ---------------------------------------------------------------------------

_STYLES = ""  # All styles now served via /static/style.css


_GOOGLE_ICON = '<svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2a10.3 10.3 0 0 0-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92a8.78 8.78 0 0 0 2.68-6.62z"/><path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.8.54-1.83.86-3.04.86-2.34 0-4.32-1.58-5.03-3.71H.96v2.33A9 9 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.97 10.71A5.41 5.41 0 0 1 3.69 9c0-.6.1-1.17.28-1.71V4.96H.96A9 9 0 0 0 0 9c0 1.45.35 2.82.96 4.04l3.01-2.33z"/><path fill="#EA4335" d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.59A9 9 0 0 0 9 0 9 9 0 0 0 .96 4.96l3.01 2.33C4.68 5.16 6.66 3.58 9 3.58z"/></svg>'
_GITHUB_ICON = '<svg width="18" height="18" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>'
_STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def _static_asset_url(filename: str) -> str:
    path = _STATIC_DIR / filename
    try:
        version = int(path.stat().st_mtime)
    except FileNotFoundError:
        return f"/static/{filename}"
    return f"/static/{filename}?v={version}"


def _page(title: str, body: str, *, nav: bool = True, extra_styles: str = "", body_class: str = "") -> str:
    nav_html = ""
    if nav:
        nav_html = """
    <div class="nav">
      <a href="/dashboard"><strong>Longhouse</strong></a>
      <div class="nav-links">
        <a href="/dashboard">Dashboard</a>
        <a href="#" onclick="fetch('/auth/logout',{method:'POST'}).then(()=>location.href='/')" class="nav-muted">Logout</a>
      </div>
    </div>"""

    extra_style_tag = f"\n    <style>{extra_styles}</style>" if extra_styles else ""
    body_attr = f' class="{html.escape(body_class)}"' if body_class else ""
    analytics_script = ""
    if settings.umami_website_id:
        domains_attr = f' data-domains="{html.escape(settings.umami_domains)}"' if settings.umami_domains else ""
        analytics_script = f"""
    <script defer src="{html.escape(settings.umami_script_src)}" data-website-id="{html.escape(settings.umami_website_id)}"{domains_attr}></script>"""
    analytics_html = f"""{analytics_script}
    <script>
      window.trackLonghouseEvent = function(name, props) {{
        try {{ window.umami && window.umami.track && window.umami.track(name, props || {{}}); }} catch (e) {{}}
      }};
    </script>"""

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="referrer" content="no-referrer">
    <title>{title} - Longhouse</title>
    <link rel="icon" href="{_static_asset_url('favicon.ico')}" />
    <link rel="icon" type="image/png" sizes="32x32" href="{_static_asset_url('favicon-32.png')}" />
    <link rel="icon" type="image/png" sizes="16x16" href="{_static_asset_url('favicon-16.png')}" />
    <link rel="apple-touch-icon" sizes="180x180" href="{_static_asset_url('apple-touch-icon.png')}" />
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <link href="https://api.fontshare.com/v2/css?f[]=general-sans@500,600,700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="{_static_asset_url('style.css')}">{extra_style_tag}{analytics_html}
  </head>
  <body{body_attr}>
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


def _csrf_token(user_id: int) -> str:
    """Daily-rotating CSRF token derived from the JWT secret and user ID."""
    day = int(time.time()) // 86400
    digest = hashlib.sha256(f"{settings.jwt_secret}:{user_id}:{day}".encode()).hexdigest()
    return digest[:32]


def _verify_csrf(user_id: int, token: str) -> bool:
    """Check current day and previous day (tolerates midnight boundary)."""
    day = int(time.time()) // 86400
    for d in (day, day - 1):
        expected = hashlib.sha256(f"{settings.jwt_secret}:{user_id}:{d}".encode()).hexdigest()[:32]
        if hmac.compare_digest(token, expected):
            return True
    return False


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def home(request: Request, error: str | None = None, return_to: str | None = None, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)

    login_action = _append_return_to("/auth/login", return_to)
    google_login_url = _append_return_to("/auth/google", return_to)
    github_login_url = _append_return_to("/auth/github", return_to)
    signup_url = _append_return_to("/signup", return_to)

    error_html = ""
    if error:
        error_html = f'<div class="alert alert-error">{html.escape(error)}</div>'

    logo_url = _static_asset_url("logo.svg")
    body = f"""
    <div class="hero-center">
      <img src="{logo_url}" alt="Longhouse" class="hero-logo" width="36" height="36">
      <h1>Longhouse</h1>
      <p class="subtitle">Welcome back.</p>
    </div>
    <div class="max-w-form">
      {error_html}
      <a href="{html.escape(google_login_url)}" class="btn btn-primary google-btn w-full">{_GOOGLE_ICON} Continue with Google</a>
      <a href="{html.escape(github_login_url)}" class="btn btn-secondary github-btn w-full" style="margin-top:0.5rem;">{_GITHUB_ICON} Continue with GitHub</a>
      <div class="divider">
        <div class="divider-line"></div>
        <span class="divider-text">or</span>
        <div class="divider-line"></div>
      </div>
    </div>
    <div class="card max-w-form">
      <form method="post" action="{html.escape(login_action)}">
        <label>Email <input type="email" name="email" required placeholder="you@example.com"></label>
        <label>Password <input type="password" name="password" required minlength="8" placeholder="&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;"></label>
        <button type="submit" class="btn btn-secondary w-full">Sign In</button>
      </form>
      <p class="text-center mt-2 text-sm text-muted">
        <a href="/forgot-password">Forgot password?</a>
        &nbsp;&middot;&nbsp;
        <a href="{html.escape(signup_url)}">Create account</a>
      </p>
    </div>
    <div class="max-w-form">
      <p class="text-center mt-6"><a href="https://longhouse.ai" class="text-muted text-sm">&larr; Back to longhouse.ai</a></p>
    </div>
    """
    return _page("Sign In", body, nav=False, body_class="page-auth")


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request, error: str | None = None, return_to: str | None = None, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)

    google_signup_url = _append_return_to("/auth/google", return_to)
    github_signup_url = _append_return_to("/auth/github", return_to)
    signin_url = _append_return_to("/", return_to)

    error_html = ""
    if error:
        error_html = f'<div class="alert alert-error">{html.escape(error)}</div>'

    logo_url = _static_asset_url("logo.svg")
    body = f"""
    <div class="hero-center">
      <img src="{logo_url}" alt="Longhouse" class="hero-logo" width="36" height="36">
      <h1>Get Hosted</h1>
      <p class="subtitle">Always-on Longhouse instance &mdash; $5/mo.</p>
    </div>
    <div class="max-w-form">
      {error_html}
      <a href="{html.escape(google_signup_url)}" class="btn btn-primary google-btn w-full"
         onclick="trackLonghouseEvent('hosted_signup_oauth_click', {{provider:'google'}})">{_GOOGLE_ICON} Continue with Google</a>
      <a href="{html.escape(github_signup_url)}" class="btn btn-secondary github-btn w-full" style="margin-top:0.5rem;"
         onclick="trackLonghouseEvent('hosted_signup_oauth_click', {{provider:'github'}})">{_GITHUB_ICON} Continue with GitHub</a>
      <div class="divider">
        <div class="divider-line"></div>
        <span class="divider-text">or</span>
        <div class="divider-line"></div>
      </div>
    </div>
    <div class="card max-w-form">
      <form method="post" action="/auth/signup" onsubmit="trackLonghouseEvent('hosted_signup_password_submit', {{surface:'signup'}})">
        <label>Email <input type="email" name="email" required placeholder="you@example.com"></label>
        <label>Password <input type="password" name="password" required minlength="8" placeholder="Min. 8 characters"></label>
        <label>Confirm password <input type="password" name="password_confirm" required minlength="8" placeholder="Repeat password"></label>
        <button type="submit" class="btn btn-secondary w-full">Create Account</button>
      </form>
      <p class="text-center mt-2 text-sm text-muted">
        Already have an account? <a href="{html.escape(signin_url)}">Sign in</a>
      </p>
    </div>
    <div class="max-w-form">
      <p class="text-center mt-6"><a href="https://longhouse.ai" class="text-muted text-sm">&larr; Back to longhouse.ai</a></p>
    </div>
    """
    return _page("Sign Up", body, nav=False, body_class="page-auth")


# ---------------------------------------------------------------------------
# Email verification page
# ---------------------------------------------------------------------------


@router.get("/verify-email", response_class=HTMLResponse)
def verify_email_page(
    request: Request, error: str | None = None, resent: str | None = None, db: Session = Depends(get_db)
):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/", status_code=302)
    if user.email_verified:
        return RedirectResponse("/dashboard", status_code=302)

    notice_html = ""
    if error:
        notice_html = f'<div class="alert alert-error">{html.escape(error)}</div>'
    elif resent:
        notice_html = '<div class="alert alert-success">Verification email resent! Check your inbox.</div>'

    body = f"""
    <div class="hero-center">
      <h1>Check Your Email</h1>
      <p class="subtitle">We sent a verification link to <strong>{html.escape(user.email)}</strong></p>
    </div>
    <div class="card max-w-form-lg">
      {notice_html}
      <p>Click the link in the email to verify your account and get started.</p>
      <p class="mt-3 text-muted text-sm">Didn't receive it? Check your spam folder or resend below.</p>
      <form method="post" action="/auth/resend-verification" class="mt-3">
        <button type="submit" class="btn btn-secondary w-full">Resend Verification Email</button>
      </form>
      <p class="text-center mt-4">
        <a href="/auth/logout?return_to=/" class="text-muted text-sm">Sign in with a different account</a>
      </p>
    </div>
    """
    return _page("Verify Email", body, nav=False, body_class="page-auth")



# ---------------------------------------------------------------------------
# Subdomain picker
# ---------------------------------------------------------------------------

_SUBDOMAIN_PICKER_EXTRA_STYLES = ""  # All styles now in /static/style.css


@router.get("/onboarding/choose-subdomain", response_class=HTMLResponse)
def choose_subdomain_page(request: Request, error: str | None = Query(default=None), db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/", status_code=302)
    if not user.email_verified:
        return RedirectResponse("/verify-email", status_code=302)
    # Already has an active instance → go to dashboard
    instance = db.query(Instance).filter(Instance.user_id == user.id).first()
    if instance and instance.status not in ("deprovisioned", "failed"):
        return RedirectResponse("/dashboard", status_code=302)

    domain = settings.root_domain
    csrf = _csrf_token(user.id)

    # Generate hint chips from the email prefix
    raw_prefix = user.email.split("@")[0].lower()
    sanitized = _re.sub(r"[^a-z0-9-]", "-", raw_prefix).strip("-")[:20]
    hints_html = ""
    if sanitized and len(sanitized) >= 3:
        hints = [sanitized]
        for i in (1, 42):
            candidate = f"{sanitized[:15]}{i}"
            if candidate != sanitized:
                hints.append(candidate)
        chips = "".join(
            f'<span class="hint-chip" onclick="fillSlug(\'{html.escape(h)}\')">{html.escape(h)}</span>'
            for h in hints
        )
        hints_html = f'<div class="mt-1"><div class="meta-label" style="margin-bottom:0.4rem;">Suggestions</div><div class="hint-row">{chips}</div></div>'

    prefill = html.escape(user.pending_subdomain or sanitized or "")

    error_html = ""
    if error:
        error_html = f'<div class="alert alert-error">{html.escape(error)}</div>'

    body = f"""
    <div class="hero-center">
      <h1>Choose your URL</h1>
      <p class="subtitle">This is the address where you'll access your Longhouse.</p>
    </div>
    <div class="card max-w-form-lg">
      {error_html}
      <form id="slug-form" method="post" action="/onboarding/set-subdomain" onsubmit="trackLonghouseEvent('hosted_subdomain_submit', {{surface:'onboarding'}})">
        <input type="hidden" name="csrf_token" value="{html.escape(csrf)}">
        <label style="margin-top:0;">Your address</label>
        <div class="slug-row" id="slug-row">
          <input class="slug-input" type="text" id="slug-input" name="subdomain"
                 value="{prefill}" required minlength="3" maxlength="63"
                 autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"
                 placeholder="myteam">
          <span class="slug-suffix">.{html.escape(domain)}</span>
        </div>
        <div class="check-badge hidden" id="check-badge"></div>

        <div class="preview-block" id="preview-block">
          <div class="preview-label">Your instance will be at</div>
          <div class="preview-url" id="preview-url"></div>
        </div>

        {hints_html}

        <button type="submit" id="submit-btn" class="btn btn-primary w-full mt-4" disabled>
          Continue to Payment &rarr;
        </button>
      </form>
      <p class="text-center mt-3 text-xs text-muted">
        You can&#8217;t change this after subscribing.
      </p>
    </div>
    <script>
    (function() {{
      const input     = document.getElementById('slug-input');
      const row       = document.getElementById('slug-row');
      const badge     = document.getElementById('check-badge');
      const preview   = document.getElementById('preview-block');
      const previewUrl= document.getElementById('preview-url');
      const submitBtn = document.getElementById('submit-btn');
      const domain    = '{html.escape(domain)}';
      let debounce, controller;

      function setAvailable(slug) {{
        row.className = 'slug-row valid';
        badge.className = 'check-badge ok';
        badge.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M3 8l3.5 3.5L13 4.5" stroke="#22c55e" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg> Available';
        previewUrl.textContent = slug + '.' + domain;
        preview.className = 'preview-block visible';
        submitBtn.disabled = false;
      }}

      function setUnavailable(reason) {{
        row.className = 'slug-row invalid';
        badge.className = 'check-badge err';
        const msgs = {{ taken: 'Already taken', reserved: 'Reserved name', invalid: 'Letters, numbers, and hyphens only (3–63 chars)' }};
        badge.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M4 4l8 8M12 4l-8 8" stroke="#f87171" stroke-width="2" stroke-linecap="round"/></svg> ' + (msgs[reason] || 'Not available');
        preview.className = 'preview-block';
        submitBtn.disabled = true;
      }}

      function setChecking() {{
        row.className = 'slug-row';
        badge.className = 'check-badge loading';
        badge.textContent = 'Checking...';
        preview.className = 'preview-block';
        submitBtn.disabled = true;
      }}

      function setEmpty() {{
        row.className = 'slug-row';
        badge.className = 'check-badge hidden';
        badge.textContent = '';
        preview.className = 'preview-block';
        submitBtn.disabled = true;
      }}

      async function check(slug) {{
        if (!slug || slug.length < 3) {{ setEmpty(); return; }}
        if (controller) controller.abort();
        controller = new AbortController();
        setChecking();
        try {{
          const res = await fetch('/api/instances/subdomain-check?subdomain=' + encodeURIComponent(slug), {{
            signal: controller.signal
          }});
          const data = await res.json();
          data.available ? setAvailable(slug) : setUnavailable(data.reason);
        }} catch (e) {{
          if (e.name !== 'AbortError') setEmpty();
        }}
      }}

      function onInput() {{
        const raw = input.value.toLowerCase().replace(/[^a-z0-9-]/g, '');
        if (raw !== input.value) {{
          const pos = input.selectionStart;
          input.value = raw;
          input.setSelectionRange(pos, pos);
        }}
        clearTimeout(debounce);
        if (!raw || raw.length < 3) {{ setEmpty(); return; }}
        // Must start and end with alphanumeric (not hyphen)
        if (!/^[a-z0-9]/.test(raw) || !/[a-z0-9]$/.test(raw)) {{
          setUnavailable('invalid');
          return;
        }}
        setChecking();
        debounce = setTimeout(() => check(raw), 320);
      }}

      input.addEventListener('input', onInput);

      // Run check on page load if there's a prefill value
      const initial = input.value.trim();
      if (initial.length >= 3) check(initial);

      // Prevent double-submit
      document.getElementById('slug-form').addEventListener('submit', (e) => {{
        if (submitBtn.disabled) e.preventDefault();
        else submitBtn.textContent = 'Saving...';
      }});
    }})();

    function fillSlug(val) {{
      const input = document.getElementById('slug-input');
      input.value = val;
      input.dispatchEvent(new Event('input'));
      input.focus();
    }}
    </script>
    """
    return _page("Choose Your URL", body, nav=False, extra_styles=_SUBDOMAIN_PICKER_EXTRA_STYLES)


@router.post("/onboarding/set-subdomain")
def set_subdomain(
    request: Request,
    subdomain: str = Form(...),
    csrf_token: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/", status_code=302)
    if not user.email_verified:
        return RedirectResponse("/verify-email", status_code=302)
    if not _verify_csrf(user.id, csrf_token):
        from urllib.parse import urlencode
        return RedirectResponse(
            f"/onboarding/choose-subdomain?{urlencode({'error': 'Session expired. Please try again.'})}",
            status_code=303,
        )

    from control_plane.routers.instances import _is_valid_subdomain
    from control_plane.routers.instances import RESERVED_SUBDOMAINS

    slug = subdomain.strip().lower()
    error = None

    if not _is_valid_subdomain(slug):
        error = "Invalid subdomain. Use 3–63 lowercase letters, numbers, or hyphens."
    elif slug in RESERVED_SUBDOMAINS:
        error = "That name is reserved. Please choose another."
    elif db.query(Instance).filter(Instance.subdomain == slug).first():
        error = "That subdomain is already taken."

    if error:
        from urllib.parse import urlencode
        params = urlencode({"error": error})
        return RedirectResponse(f"/onboarding/choose-subdomain?{params}", status_code=303)

    user.pending_subdomain = slug
    db.commit()
    return RedirectResponse("/dashboard", status_code=303)


# ---------------------------------------------------------------------------
# Password reset pages
# ---------------------------------------------------------------------------


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request, sent: str | None = None, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)

    notice_html = ""
    if sent:
        notice_html = (
            '<div class="alert alert-success">'
            "If an account exists with that email, we've sent a password reset link. Check your inbox."
            "</div>"
        )

    body = f"""
    <div class="hero-center">
      <h1>Forgot Password</h1>
      <p class="subtitle">Enter your email and we'll send you a reset link.</p>
    </div>
    <div class="card max-w-form">
      {notice_html}
      <form method="post" action="/auth/reset-password-request">
        <label>Email <input type="email" name="email" required placeholder="you@example.com"></label>
        <button type="submit" class="btn btn-primary w-full">Send Reset Link</button>
      </form>
      <p class="text-center mt-2 text-sm text-muted">
        Remember your password? <a href="/">Sign in</a>
      </p>
    </div>
    """
    return _page("Forgot Password", body, nav=False, body_class="page-auth")


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(
    request: Request, token: str | None = None, error: str | None = None, db: Session = Depends(get_db)
):
    error_html = ""
    if error:
        error_html = f'<div class="alert alert-error">{html.escape(error)}</div>'

    if not token and not error:
        return RedirectResponse("/forgot-password", status_code=302)

    if not token:
        body = f"""
        <div class="hero-center">
          <h1>Reset Password</h1>
        </div>
        <div class="card max-w-form">
          {error_html}
          <p class="text-center mt-2">
            <a href="/forgot-password" class="btn btn-primary">Request New Reset Link</a>
          </p>
        </div>
        """
        return _page("Reset Password", body, nav=False, body_class="page-auth")

    token_escaped = html.escape(token)
    body = f"""
    <div class="hero-center">
      <h1>Reset Password</h1>
      <p class="subtitle">Choose a new password for your account.</p>
    </div>
    <div class="card max-w-form">
      {error_html}
      <form method="post" action="/auth/reset-password">
        <input type="hidden" name="token" value="{token_escaped}">
        <label>New password <input type="password" name="password" required minlength="8" placeholder="Min. 8 characters"></label>
        <label>Confirm new password <input type="password" name="password_confirm" required minlength="8" placeholder="Repeat password"></label>
        <button type="submit" class="btn btn-primary w-full">Reset Password</button>
      </form>
      <p class="text-center mt-2 text-sm text-muted">
        <a href="/">Back to sign in</a>
      </p>
    </div>
    """
    return _page("Reset Password", body, nav=False, body_class="page-auth")


# ---------------------------------------------------------------------------
# Authenticated pages
# ---------------------------------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/", status_code=302)

    instance = db.query(Instance).filter(Instance.user_id == user.id).first()

    unverified_banner = ""
    if not user.email_verified:
        unverified_banner = f"""
        <div class="alert alert-warning">
          Please verify your email (<strong>{html.escape(user.email)}</strong>) to enable billing and account recovery.
          <form method="post" action="/auth/resend-verification" style="display:inline;margin-left:0.5rem;">
            <button type="submit" class="btn-inline-link">Resend email</button>
          </form>
        </div>"""

    if instance and instance.status not in ("deprovisioned", "failed"):
        instance_url = f"https://{instance.subdomain}.{settings.root_domain}"
        status_class = f"status-{instance.status}" if instance.status in ("active", "provisioning", "canceled") else ""

        billing_btn = (
            '<a href="/billing/portal-redirect" class="btn btn-secondary">Manage Billing</a>'
            if user.stripe_customer_id else ""
        )

        body = f"""{unverified_banner}
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
        return RedirectResponse("/provisioning", status_code=302)
    else:
        # Auto-derive a subdomain suggestion if user hasn't chosen one yet
        if not user.pending_subdomain:
            raw_prefix = user.email.split("@")[0].lower()
            user.pending_subdomain = _re.sub(r"[^a-z0-9-]", "-", raw_prefix).strip("-")[:20] or "user"
            db.commit()

        domain = settings.root_domain
        chosen_url = f"{html.escape(user.pending_subdomain)}.{html.escape(domain)}"
        body = f"""{unverified_banner}
        <h1>Get Started</h1>
        <p class="subtitle">Launch your own always-on Longhouse instance.</p>
        <div class="card">
          <div class="mt-1">
            <div class="meta-label">Your instance URL</div>
            <div class="actions mt-1">
              <span class="url-badge">{chosen_url}</span>
              <a href="/onboarding/choose-subdomain" class="text-xs text-muted">Customize</a>
            </div>
          </div>
          <h2 class="mt-3">Hosted &mdash; $5/mo</h2>
          <p>Always-on instance, automatic updates, access from any device.</p>
          <form method="post" action="/dashboard/checkout" onsubmit="trackLonghouseEvent('hosted_checkout_start', {{plan:'hosted_5'}})">
            <button type="submit" class="btn btn-primary mt-3">Subscribe &amp; Launch Instance</button>
          </form>
        </div>
        <p class="text-center mt-4">
          <a href="https://longhouse.ai" class="text-muted text-sm">Or self-host free forever &rarr;</a>
        </p>
        """

    return _page("Dashboard", body)


@router.get("/dashboard/open-instance", response_class=HTMLResponse)
def open_instance(request: Request, return_to: str | None = Query(default=None), db: Session = Depends(get_db)):
    """Issue a tenant login token and redirect the browser back to the instance."""
    user = _get_user_from_cookie(request, db)
    if not user:
        current_path = request.url.path
        if request.url.query:
            current_path = f"{current_path}?{request.url.query}"
        return RedirectResponse(_append_return_to("/", current_path), status_code=302)

    instance = db.query(Instance).filter(Instance.user_id == user.id).first()
    if not instance:
        return RedirectResponse("/dashboard", status_code=302)

    instance_url = f"https://{instance.subdomain}.{settings.root_domain}"

    token = _issue_instance_sso_token(user=user, instance=instance)

    handoff_url = f"{instance_url}/api/auth/accept-token?token={urllib.parse.quote(token, safe='')}"
    safe_return_to = normalize_local_return_to(return_to)
    if safe_return_to:
        handoff_url = f"{handoff_url}&return_to={urllib.parse.quote(safe_return_to, safe='')}"
    return RedirectResponse(handoff_url, status_code=302)


@router.post("/dashboard/checkout")
def dashboard_checkout(request: Request, db: Session = Depends(get_db)):
    """Trigger Stripe checkout from dashboard — redirect to Stripe."""
    user = _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/", status_code=302)

    # Auto-derive subdomain if not set
    if not user.pending_subdomain:
        raw_prefix = user.email.split("@")[0].lower()
        user.pending_subdomain = _re.sub(r"[^a-z0-9-]", "-", raw_prefix).strip("-")[:20] or "user"
        db.commit()

    from control_plane.routers.billing import _create_checkout_session

    session = _create_checkout_session(
        user,
        db,
        cancel_url=f"https://control.{settings.root_domain}/dashboard",
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
        body = """
        <div class="card text-center pad-hero">
          <div class="spinner"></div>
          <h2 class="mt-3">Setting up your instance</h2>
          <p>Waiting for payment confirmation...</p>
          <p><small>This page refreshes automatically.</small></p>
        </div>
        <script>setTimeout(() => location.reload(), 5000);</script>
        """
        return _page("Provisioning", body)

    if instance.status == "active":
        return RedirectResponse("/dashboard/open-instance", status_code=302)

    if instance.status == "failed":
        body = """
        <div class="card text-center pad-hero">
          <h2>Something went wrong</h2>
          <p>Your instance failed to provision. We've been notified.</p>
          <div class="actions" style="justify-content:center">
            <a href="mailto:hello@longhouse.ai?subject=Provisioning%20failure" class="btn btn-primary">Contact Support</a>
            <a href="/dashboard" class="btn btn-secondary">Back</a>
          </div>
        </div>
        """
        return _page("Provisioning Failed", body)

    # Provisioning in progress — poll server-side health check.
    # On success, redirect through /dashboard/open-instance which issues a
    # tenant login token and returns the browser to the instance.
    instance_host = f"{instance.subdomain}.{settings.root_domain}"

    body = f"""
    <div class="card text-center pad-hero">
      <div class="spinner" id="spinner"></div>
      <h2 class="mt-3" id="status-text">Starting your instance</h2>
      <p><code class="url-badge">{instance_host}</code></p>
      <p id="sub-text" class="text-muted text-sm mt-1">
        Setting up SSL certificate and waiting for services to start...
      </p>
    </div>
    <script>
      let attempts = 0;

      async function checkHealth() {{
        attempts++;
        try {{
          const resp = await fetch('/api/instances/me/health', {{ credentials: 'same-origin' }});
          const data = await resp.json();
          if (data.ready) {{
            document.getElementById('status-text').textContent = 'Instance is ready! Redirecting...';
            document.getElementById('sub-text').textContent = '';
            document.getElementById('spinner').style.display = 'none';
            // Redirect through the hosted auth handoff — auto-authenticates the user on their instance
            setTimeout(() => window.location.href = '/dashboard/open-instance', 1500);
            return;
          }}
        }} catch (e) {{
          // Control plane request failed — retry
        }}

        if (attempts < 90) {{
          setTimeout(checkHealth, 4000);
        }} else {{
          document.getElementById('status-text').textContent = 'Taking longer than expected. Check back in a minute.';
          document.getElementById('sub-text').textContent = '';
          document.getElementById('spinner').style.display = 'none';
        }}
      }}

      setTimeout(checkHealth, 5000);
    </script>
    """
    return _page("Provisioning", body)


# ---------------------------------------------------------------------------
# Admin pages (existing, unchanged)
# ---------------------------------------------------------------------------


@router.get("/admin", response_class=HTMLResponse)
def admin(show_all: bool = Query(False), db: Session = Depends(get_db)):
    rows = db.query(Instance, User).join(User, Instance.user_id == User.id).all()
    table_rows_rendered: list[str] = []
    hidden_count = 0
    for inst, user in rows:
        is_hidden_test_row = (
            not show_all
            and inst.status == "deprovisioned"
            and inst.subdomain.startswith("e2e-")
        )
        if is_hidden_test_row:
            hidden_count += 1
            continue

        migration = _build_migration_status(inst)
        if migration.state == "pending":
            migration_text = f"pending ({migration.pending_count})"
        elif migration.state == "ok":
            migration_text = "ok"
        else:
            migration_text = migration.state
        table_rows_rendered.append(
            f"<tr><td>{inst.id}</td><td>{html.escape(user.email)}</td>"
            f"<td>{html.escape(inst.subdomain)}</td><td>{inst.status}</td>"
            f"<td>{html.escape(migration_text)}</td></tr>"
        )
    table_rows = "".join(table_rows_rendered)
    if not table_rows:
        table_rows = '<tr><td colspan=5><em>No instances yet</em></td></tr>'

    if hidden_count > 0:
        hidden_note = (
            f'<p><small>Hiding {hidden_count} deprovisioned test instances. '
            f'<a href="/admin?show_all=1">Show all</a></small></p>'
        )
    elif show_all:
        hidden_note = '<p><small><a href="/admin">Hide deprovisioned test instances</a></small></p>'
    else:
        hidden_note = ""

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
      {hidden_note}
      <table>
        <thead><tr><th>ID</th><th>Email</th><th>Subdomain</th><th>Status</th><th>Migrations</th></tr></thead>
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
