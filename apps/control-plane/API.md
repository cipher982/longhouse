# Control Plane API (v0)

## Public

- `GET /health` → `{ status: "ok" }`
- `GET /` → minimal HTML status page
- `GET /admin` → minimal HTML provisioning page

## Admin (requires `X-Admin-Token` header or form field)

- `GET /api/instances` → list instances
- `POST /api/instances`
  - Body: `{ "email": "user@example.com", "subdomain": "alice" }`
  - Provisions a user instance (idempotent on subdomain)
- `GET /api/instances/{instance_id}` → instance status
- `POST /api/instances/{instance_id}/deprovision` → stop/remove instance (optional retain volume)
- `POST /api/instances/{instance_id}/login-token`
  - Body: `{ "email": "user@example.com" }`
  - Returns a short-lived control-plane signed token for instance login

## Webhooks (stub)

- `POST /webhooks/stripe` → validates signature, queues provisioning (not implemented yet)

## Future (not implemented yet)

- OAuth flow (`/auth/google`)
- Billing portal (`/billing/portal`)
