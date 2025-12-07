# Swarm Platform Auth Contract

**Version:** 1.0
**Status:** Draft

## Cookie Specification

| Property | Value              |
| -------- | ------------------ |
| Name     | `swarm_session`    |
| HttpOnly | `true`             |
| SameSite | `Lax`              |
| Secure   | `true` (prod only) |
| Path     | `/`                |
| Max-Age  | `43200` (12 hours) |

## JWT Payload

```json
{
  "sub": "<user_id>",
  "iss": "device" | "google",
  "iat": <issued_at_unix>,
  "exp": <expiry_unix>
}
```

## Issuance

### Dev Mode (Current)

- Endpoint: `POST /api/jarvis/auth`
- Request: `{ "device_secret": "<secret>" }`
- Response: Sets `swarm_session` cookie with `iss: "device"`

### Prod Mode (Future)

- Endpoint: Google OAuth callback
- Response: Sets `swarm_session` cookie with `iss: "google"`

## Validation

Both `jarvis-server` and `zerg-backend` MUST:

1. Read `swarm_session` cookie
2. Verify JWT signature
3. Check `exp` not passed
4. Extract `sub` as authenticated user ID
5. Reject if no cookie or invalid

## Security Notes

- Device secret scoped to allowed domain only
- Rotate secret periodically
- No refresh tokens in browser (server-side only for OAuth)
- CSRF: SameSite=Lax provides protection for state-changing requests
