# Frontend Unification Implementation Guide

**Version:** 1.0
**Date:** December 2024
**Status:** Ready for Implementation

---

## Overview

This document provides step-by-step implementation instructions for unifying the Jarvis and Zerg frontends at the nginx proxy layer. This is the low-risk, incremental approach that preserves both codebases while gaining same-origin benefits.

### Goals

1. Single entry point (one port) for users
2. Same-origin API calls (no CORS complexity)
3. Unified branding (favicon/logo)
4. Rollback capability
5. Auth contract documented for future OAuth

### Non-Goals (Deferred)

- React port of Jarvis
- Codebase merge
- Bundle optimization

---

## Phase 0: Auth Contract Documentation

**Duration:** 30 minutes
**Risk:** None (documentation only)

### Task 0.1: Create Auth Contract Spec

Create file `docs/specs/auth-contract.md` with the following content:

````markdown
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
````

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

````

### Verification

- [ ] File created at `docs/specs/auth-contract.md`
- [ ] Content matches specification above

---

## Phase 1: Nginx Proxy Unification

**Duration:** 1-2 hours
**Risk:** Medium (routing changes)
**Rollback:** Comment block + env flag

### Task 1.1: Update Nginx Config

Edit `docker/nginx/docker-compose.unified.conf`:

**Current state:** Two server blocks (port 80 for Jarvis, port 81 for Zerg)
**Target state:** Single server block (port 80) with path-based routing

Replace the entire file with:

```nginx
# Nginx reverse proxy configuration for unified Swarm Platform
# Single entry point with path-based routing

# Upstream definitions for clarity
upstream jarvis_web {
    server jarvis-web:8080;
}

upstream jarvis_server {
    server jarvis-server:8787;
}

upstream zerg_frontend {
    server zerg-frontend:5173;
}

upstream zerg_backend {
    server zerg-backend:8000;
}

# Main server block - single entry point
server {
    listen 80;
    server_name _;

    access_log /var/log/nginx/unified-access.log;
    error_log /var/log/nginx/unified-error.log;

    # =========================================================================
    # Health Check
    # =========================================================================
    location /health {
        access_log off;
        return 200 "healthy\n";
        add_header Content-Type text/plain;
    }

    # =========================================================================
    # Jarvis Chat UI - /chat/*
    # =========================================================================
    location /chat {
        proxy_pass http://jarvis_web;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }

    # =========================================================================
    # Zerg Dashboard - /dashboard/*
    # =========================================================================
    location /dashboard {
        proxy_pass http://zerg_frontend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }

    # Zerg Agent/Thread pages
    location /agent {
        proxy_pass http://zerg_frontend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }

    # Zerg other authenticated routes
    location ~ ^/(canvas|profile|settings|admin) {
        proxy_pass http://zerg_frontend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }

    # =========================================================================
    # Jarvis Server API (OpenAI Realtime bridge)
    # =========================================================================
    location /api/session {
        proxy_pass http://jarvis_server/session;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/tool {
        proxy_pass http://jarvis_server/tool;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/sync {
        proxy_pass http://jarvis_server/sync;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/sync/ {
        proxy_pass http://jarvis_server/sync/;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # =========================================================================
    # Zerg Backend API
    # =========================================================================

    # SSE endpoints - special handling for long-lived connections
    location ~ ^/api/jarvis/supervisor/events {
        proxy_pass http://zerg_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE-specific settings
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400s;  # 24 hours
        proxy_send_timeout 86400s;
        chunked_transfer_encoding off;
    }

    # WebSocket endpoint
    location /api/ws {
        proxy_pass http://zerg_backend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket timeouts
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }

    # Legacy /ws path
    location /ws {
        proxy_pass http://zerg_backend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }

    # All other API routes to Zerg backend
    location /api/ {
        proxy_pass http://zerg_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # =========================================================================
    # Marketing/Public Pages - Root and static routes
    # Served by Zerg frontend (has landing page, pricing, docs, etc.)
    # =========================================================================
    location / {
        proxy_pass http://zerg_frontend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }
}

# =========================================================================
# LEGACY: Port 81 server block (for rollback)
# Uncomment to restore old two-port behavior if rollback is needed
# =========================================================================
# server {
#     listen 81;
#     server_name _;
#
#     location / {
#         proxy_pass http://zerg_frontend;
#         # ... (copy old config here for rollback)
#     }
# }
````

### Task 1.2: Update Docker Compose

Edit `docker/docker-compose.unified.yml`:

1. Remove port 81 from reverse-proxy:

```yaml
 reverse-proxy:
    image: nginx:alpine
    restart: unless-stopped
    stop_grace_period: 1s
    environment:
      <<: *common-variables
    ports:
      - "${JARPXY_PORT}:80"      # Single entry point
      # Removed: - "${ZGPXY_PORT}:81"
```

### Task 1.3: Update Dev Script

Edit `scripts/dev-docker.sh`:

1. Remove ZGPXY_PORT export (or keep for backward compat):

```bash
# Change the success message
echo -e "${GREEN}✅ Development environment ready!${NC}"
echo -e "${BLUE}   App:        http://localhost:30080${NC}"
echo -e "${BLUE}   Chat:       http://localhost:30080/chat${NC}"
echo -e "${BLUE}   Dashboard:  http://localhost:30080/dashboard${NC}"
```

### Task 1.4: Update Zerg Frontend Routes

Edit `apps/zerg/frontend-web/src/routes/App.tsx`:

Add `/chat` route that redirects or shows a "go to chat" message:

```tsx
// Add import
import { Navigate } from "react-router-dom";

// Add route in the routes array (before the fallback):
{
  path: "/chat",
  element: (
    <ErrorBoundary>
      {/* For now, this will be handled by nginx routing to jarvis-web */}
      {/* This route exists as fallback if someone hits it directly in React */}
      <Navigate to="/" replace />
    </ErrorBoundary>
  )
},
```

**Note:** The actual `/chat` will be served by jarvis-web via nginx. This React route is just a fallback.

### Task 1.5: Update Jarvis Web Base Path

Edit `apps/jarvis/apps/web/vite.config.ts`:

```typescript
export default defineConfig({
  base: "/chat/", // Add this line
  build: {
    outDir: "./dist",
    // ...
  },
  // ...
});
```

Edit `apps/jarvis/apps/web/index.html`:

Update asset references to be relative or use base path:

```html
<link rel="icon" href="icon-192.png" />
<!-- becomes -->
<link rel="icon" href="/chat/icon-192.png" />
```

### Task 1.6: Update Jarvis API Calls

Edit `apps/jarvis/apps/web/lib/task-inbox.ts` (and any other files calling Zerg API):

Change from:

```typescript
apiURL: import.meta.env?.VITE_ZERG_API_URL || 'http://localhost:47300',
```

To:

```typescript
apiURL: '/api',  // Same-origin now
```

Search for any `VITE_ZERG_API_URL` references and update to relative paths.

### Verification Checklist - Phase 1

- [ ] Nginx config updated with single server block
- [ ] Docker compose updated (single port)
- [ ] Dev script updated with new URLs
- [ ] Zerg frontend has /chat route handler
- [ ] Jarvis vite.config.ts has base: '/chat/'
- [ ] Jarvis API calls use relative paths
- [ ] `make dev` starts successfully
- [ ] http://localhost:30080/ shows Zerg landing page
- [ ] http://localhost:30080/chat shows Jarvis UI
- [ ] http://localhost:30080/dashboard shows Zerg dashboard
- [ ] http://localhost:30080/api/health returns healthy

---

## Phase 2: Favicon and Branding Unification

**Duration:** 30 minutes
**Risk:** Low

### Task 2.1: Create Jarvis Public Directory

```bash
mkdir -p apps/jarvis/apps/web/public
```

### Task 2.2: Copy Favicon Assets

```bash
cp apps/zerg/frontend-web/public/favicon.ico apps/jarvis/apps/web/public/
cp apps/zerg/frontend-web/public/favicon-16.png apps/jarvis/apps/web/public/
cp apps/zerg/frontend-web/public/favicon-32.png apps/jarvis/apps/web/public/
cp apps/zerg/frontend-web/public/favicon-512.png apps/jarvis/apps/web/public/
cp apps/zerg/frontend-web/public/apple-touch-icon.png apps/jarvis/apps/web/public/
cp apps/zerg/frontend-web/public/maskable-icon-192.png apps/jarvis/apps/web/public/
cp apps/zerg/frontend-web/public/maskable-icon-512.png apps/jarvis/apps/web/public/
cp apps/zerg/frontend-web/public/og-image.png apps/jarvis/apps/web/public/
```

### Task 2.3: Create Jarvis Web Manifest

Create `apps/jarvis/apps/web/public/site.webmanifest`:

```json
{
  "name": "Jarvis - Swarm AI Assistant",
  "short_name": "Jarvis",
  "description": "Voice and text AI assistant powered by Swarm",
  "icons": [
    {
      "src": "/chat/maskable-icon-192.png",
      "sizes": "192x192",
      "type": "image/png",
      "purpose": "any maskable"
    },
    {
      "src": "/chat/maskable-icon-512.png",
      "sizes": "512x512",
      "type": "image/png",
      "purpose": "any maskable"
    }
  ],
  "theme_color": "#0a0a0f",
  "background_color": "#0a0a0f",
  "display": "standalone",
  "start_url": "/chat/",
  "scope": "/chat/"
}
```

### Task 2.4: Update Jarvis index.html

Edit `apps/jarvis/apps/web/index.html`:

Replace the `<head>` section:

```html
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Jarvis - Swarm AI Assistant</title>

  <!-- Favicon -->
  <link rel="icon" href="/chat/favicon.ico?v=2" />
  <link
    rel="icon"
    type="image/png"
    sizes="32x32"
    href="/chat/favicon-32.png?v=2"
  />
  <link
    rel="icon"
    type="image/png"
    sizes="16x16"
    href="/chat/favicon-16.png?v=2"
  />
  <link
    rel="apple-touch-icon"
    sizes="180x180"
    href="/chat/apple-touch-icon.png?v=2"
  />

  <!-- PWA -->
  <link rel="manifest" href="/chat/site.webmanifest" />
  <meta name="theme-color" content="#0a0a0f" />

  <!-- Fonts -->
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link
    href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap"
    rel="stylesheet"
  />

  <!-- SEO / Open Graph -->
  <meta
    name="description"
    content="Voice and text AI assistant powered by Swarm"
  />
  <meta property="og:type" content="website" />
  <meta property="og:site_name" content="Swarm" />
  <meta property="og:title" content="Jarvis - Swarm AI Assistant" />
  <meta
    property="og:description"
    content="Voice and text AI assistant powered by Swarm"
  />
  <meta property="og:image" content="/chat/og-image.png" />
  <meta name="twitter:card" content="summary_large_image" />

  <!-- Analytics (Umami) -->
  <script
    defer
    src="https://analytics.drose.io/script.js"
    data-website-id="486eaa80-2916-41ee-a2a2-f55209495028"
  ></script>
</head>
```

### Task 2.5: Delete Old Icon Files

```bash
rm apps/jarvis/apps/web/icon-192.png
rm apps/jarvis/apps/web/icon-512.png
```

### Verification Checklist - Phase 2

- [ ] `public/` directory created in jarvis web
- [ ] All favicon assets copied
- [ ] `site.webmanifest` created with correct paths
- [ ] `index.html` updated with new favicon links
- [ ] Old icon files deleted
- [ ] Browser shows new purple robot favicon at /chat
- [ ] PWA install works with correct icon

---

## Rollback Procedure

If something goes wrong:

### Quick Rollback (nginx only)

1. Edit `docker/nginx/docker-compose.unified.conf`
2. Uncomment the legacy port 81 server block
3. Add port 81 back to docker-compose reverse-proxy
4. Run `docker compose -f docker/docker-compose.unified.yml restart reverse-proxy`

### Full Rollback

1. `git checkout docker/nginx/docker-compose.unified.conf`
2. `git checkout docker/docker-compose.unified.yml`
3. `git checkout apps/jarvis/apps/web/`
4. `make stop && make dev`

---

## Testing Checklist

After implementation, verify:

### Routing

- [ ] `http://localhost:30080/` → Zerg landing page
- [ ] `http://localhost:30080/chat` → Jarvis chat UI
- [ ] `http://localhost:30080/dashboard` → Zerg dashboard
- [ ] `http://localhost:30080/agent/1` → Agent chat page
- [ ] `http://localhost:30080/api/health` → Backend health

### Jarvis Functionality

- [ ] Voice PTT mode works
- [ ] Text input works
- [ ] Conversations persist (IndexedDB)
- [ ] Supervisor delegation works (if configured)
- [ ] SSE events stream correctly

### Branding

- [ ] Favicon shows purple robot on all pages
- [ ] PWA install shows correct icon
- [ ] OG image correct for social sharing

### No Regressions

- [ ] Zerg dashboard still works
- [ ] Agent chat still works
- [ ] WebSocket connections work
- [ ] No CORS errors in console

---

## Next Steps (Future Phases)

After this implementation is stable:

1. **Phase 3:** Implement auth contract (unified cookie)
2. **Phase 4:** Add navigation links between /chat and /dashboard
3. **Phase 5:** Playwright smoke tests
4. **Phase 6:** Consider React port (only if needed)

---

_End of Implementation Guide_
