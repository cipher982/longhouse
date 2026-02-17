# Control Plane API (v0)

## Public

- `GET /health` → `{ "status": "ok" }`
- `GET /` → control plane landing page
- `GET /admin` → minimal HTML provisioning form (admin token required on submit)

## Auth

- `POST /auth/signup` → create user (email+password), send verification email
- `POST /auth/login` → verify email+password, set session cookie
- `GET /auth/google` → redirect to Google OAuth
- `GET /auth/google/callback` → exchange code, set session cookie
- `GET /auth/verify?token=...` → verify email, set session cookie
- `POST /auth/resend-verification` → resend verification email
- `GET /auth/status` → check authentication
- `POST /auth/logout` → clear session cookie

## Dashboard UI

- `GET /dashboard` → instance status + billing actions
- `GET /provisioning` → provisioning status + auto-redirect to instance
- `GET /dashboard/open-instance` → issue short-lived SSO token + redirect to instance

## Billing (Stripe)

- `POST /billing/checkout` → create Stripe Checkout session
- `POST /billing/portal` → create Stripe billing portal session
- `GET /billing/portal-redirect` → UI redirect into billing portal

## Webhooks

- `POST /webhooks/stripe` → Stripe webhook handler (provisioning + subscription updates)

## Instances (Admin)

- `GET /api/instances` → list instances
- `POST /api/instances`
  - Body: `{ "email": "user@example.com", "subdomain": "alice" }`
  - Provisions a user instance (idempotent on subdomain)
- `GET /api/instances/{instance_id}` → instance status
- `POST /api/instances/{instance_id}/deprovision` → stop/remove instance
- `POST /api/instances/{instance_id}/reprovision` → stop/remove + recreate instance
- `POST /api/instances/{instance_id}/regenerate-password` → rotate instance password
- `POST /api/instances/{instance_id}/login-token` → short-lived instance login token

## Instances (User)

- `GET /api/instances/me` → current user's instance
- `GET /api/instances/me/health` → server-side health probe, updates instance status
- `GET /api/instances/sso-keys` → instance SSO signing keys (instance-authenticated)
