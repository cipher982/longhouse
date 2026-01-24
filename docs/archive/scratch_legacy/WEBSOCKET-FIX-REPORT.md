# WebSocket Connection Failure - Root Cause Analysis & Fix
**Date**: 2025-11-27
**Status**: ✅ RESOLVED
**Severity**: Critical - Blocked all workflow execution

---

## Executive Summary

WebSocket connections were failing with "403 Forbidden" errors in production, preventing real-time workflow updates. The issue had **two root causes**:

1. **Frontend misconfiguration** - Connecting to wrong WebSocket URL
2. **Caddy proxy interference** - Coolify's auto-generated labels broke WebSocket upgrades

Both issues are now resolved with robust, deployment-proof solutions.

---

## Problem Statement

### Symptoms
Users reported workflows failing to execute with console errors:
```
WebSocket connection to 'wss://swarmlet.com/api/ws?token=...' failed:
WebSocket is closed before the connection is established.
```

### Impact
- ❌ No real-time workflow updates
- ❌ Workflow execution panel stuck/frozen
- ❌ Poor user experience for core product feature

---

## Root Cause Analysis

### Cause #1: Frontend Connecting to Wrong URL

**The Problem:**
Frontend at `https://swarmlet.com` was trying to connect to `wss://swarmlet.com/api/ws`.

**Why This Failed:**
```
┌─────────────────────────────────────────────────┐
│  Coolify Deployment Architecture               │
├─────────────────────────────────────────────────┤
│                                                 │
│  Frontend Container (swarmlet.com)             │
│  ├── Nginx serving React SPA                   │
│  └── Tries to proxy /api/ws to "backend"       │
│      └── ❌ "backend" hostname not resolvable   │
│          (separate Coolify service)            │
│                                                 │
│  Backend Container (api.swarmlet.com)          │
│  ├── FastAPI on port 8000                      │
│  └── WebSocket endpoint at /api/ws             │
│                                                 │
└─────────────────────────────────────────────────┘
```

In local development, `docker-compose` creates a shared network where `backend` hostname resolves. In Coolify production, frontend and backend are **separate services** with no shared network, so nginx can't proxy to `backend`.

**The Fix:**
Updated `apps/zerg/frontend-web/public/config.js` to connect directly to the backend domain:

```javascript
// Before:
window.WS_BASE_URL = window.location.origin.replace("http", "ws");
// Result: wss://swarmlet.com/api/ws ❌

// After:
if (window.location.hostname === 'swarmlet.com') {
  window.WS_BASE_URL = "wss://api.swarmlet.com";
}
// Result: wss://api.swarmlet.com/api/ws ✅
```

---

### Cause #2: Caddy Proxy Breaking WebSocket Upgrades

**The Problem:**
Even with correct URL, WebSocket requests to `wss://api.swarmlet.com/api/ws` returned 403.

**Investigation Timeline:**

1. **Tested endpoint directly** - Backend WebSocket handler works correctly
2. **Checked Caddy logs** - 403 coming from proxy, not backend
3. **Inspected Docker labels** - Found problematic Coolify auto-generated config:
   ```yaml
   caddy_0.handle_path=/*
   caddy_0.handle_path.0_reverse_proxy={{upstreams 8000}}
   caddy_0.try_files={path} /index.html /index.php  # ❌ BREAKS WEBSOCKET
   ```

4. **Analyzed generated Caddy config**:
   ```json
   {
     "handler": "subroute",
     "routes": [
       {"handler": "headers"},
       {
         "handler": "rewrite",              // ❌ try_files runs BEFORE proxy
         "match": [{"file": {"try_files": [...]}}]
       },
       {"handler": "encode"},
       {"handler": "reverse_proxy"}         // Never reached for WS
     ]
   }
   ```

**Why `try_files` Breaks WebSocket:**

The `try_files` directive checks if files exist on disk before proxying. For WebSocket upgrade requests:
1. Request arrives with `Connection: Upgrade` header
2. Caddy's file matcher checks for `/api/ws` file
3. File doesn't exist → request doesn't match → falls through to 403
4. `reverse_proxy` never executes

This is a **Coolify design flaw** - it adds `try_files` to ALL services (meant for SPAs) without checking if it's an API backend.

**Why Our Label Override Didn't Work:**

We tried adding clean labels in `docker-compose.prod.yml`:
```yaml
labels:
  - "caddy_0.reverse_proxy={{upstreams 8000}}"
```

But Coolify **merges** labels instead of replacing them, so we got BOTH:
- Our clean `reverse_proxy`
- Coolify's broken `handle_path` + `try_files`

Result: Same 403 error.

---

## Solution Architecture

### Frontend Fix (Permanent)
**File**: `apps/zerg/frontend-web/public/config.js`
**Commit**: `cb1680a`

```javascript
if (window.location.hostname === 'swarmlet.com') {
  window.WS_BASE_URL = "wss://api.swarmlet.com";
}
```

**Why Robust:**
- ✅ Hardcoded for production domain
- ✅ Survives rebuilds (in source control)
- ✅ Falls back to VITE_WS_BASE_URL in development

---

### Caddy Override Fix (Manual, Persistent)
**File**: `/data/coolify/proxy/caddy/dynamic/api-swarmlet-websocket.caddy` (on zerg server)

```caddy
# WebSocket fix for api.swarmlet.com
# Overrides Coolify's Docker label config which includes try_files

https://api.swarmlet.com {
    encode zstd gzip
    header -Server
    reverse_proxy backend:8000
}
```

**How It Works:**
1. Caddy scans Docker labels AND files in `/dynamic/`
2. Finds TWO definitions for `api.swarmlet.com` (Docker labels + our file)
3. Detects "ambiguous site definition"
4. **Removes the Docker label config** (the broken one)
5. Uses our clean manual file

**Why Robust:**
- ✅ Persists through Coolify redeployments (not in managed compose file)
- ✅ Uses stable hostname `backend` (Docker Compose service name)
- ✅ Simple config - just `reverse_proxy` (handles HTTP + WebSocket automatically)
- ✅ No complex matchers or paths to break
- ✅ Survives container recreation (service name doesn't change)

---

## Verification & Testing

### Test Results (2025-11-27)

**HTTP Endpoint:**
```bash
curl -s https://api.swarmlet.com/api/system/health
# Result: {"status":"ok","ws":{"available":true,"active_connections":2,"topics":3}}
```
✅ WebSocket infrastructure operational with active connections

**WebSocket Connection:**
```bash
websocat "wss://api.swarmlet.com/api/ws?token=invalid"
# Result: 403 Forbidden (expected - invalid token)
```
✅ Requests reaching backend correctly (auth working as designed)

**Backend Logs:**
```
INFO: ('172.19.0.2', 41400) - "WebSocket /api/ws?token=test" 403
INFO: connection rejected (403 Forbidden)
```
✅ WebSocket requests passing through Caddy to FastAPI

**Production Test:**
User loaded https://swarmlet.com and ran workflow - no WebSocket errors.
✅ Real-time updates working

---

## Deployment Impact

### Changes Merged to `main`

1. **Frontend config fix** (cb1680a) - Auto-deploys via Coolify webhook
2. **CSP font fixes** (0b82baf) - Auto-deploys via Coolify webhook

### Manual Server Configuration

**One-time setup on zerg server:**
```bash
# Created: /data/coolify/proxy/caddy/dynamic/api-swarmlet-websocket.caddy
# Persists through: Container restarts, Coolify redeployments, code pushes
# Only lost if: Server rebuilt from scratch or file manually deleted
```

**If server is ever rebuilt:**
1. SSH to server: `ssh zerg`
2. Recreate Caddy override file:
   ```bash
   sudo tee /data/coolify/proxy/caddy/dynamic/api-swarmlet-websocket.caddy << 'EOF'
   https://api.swarmlet.com {
       encode zstd gzip
       header -Server
       reverse_proxy backend:8000
   }
   EOF
   ```
3. Restart Caddy: `docker restart coolify-proxy`

---

## Infrastructure Improvements Made

### Docker Permissions Fixed
**Issue**: `zerg` user couldn't access Docker logs for debugging

**Fix:**
```bash
ssh zerg
sudo usermod -aG docker zerg
```

**Result**: Can now debug production issues via SSH without requiring Coolify UI

---

## Lessons Learned

### 1. Coolify's Label System is Opinionated
- Automatically adds `try_files` to all services
- Designed for SPAs, breaks API backends
- Labels merge rather than override
- Manual Caddy files are the escape hatch

### 2. WebSocket Routing Requires Special Care
- HTTP/2 doesn't support WebSocket (needs HTTP/1.1)
- File-based matchers (`try_files`) break upgrades
- Simple `reverse_proxy` is best for APIs
- Caddy handles WebSocket automatically if route matches

### 3. Container Networking in Coolify
- Services are isolated (can't reach each other by compose service name)
- Must connect via public domains or shared networks
- Frontend nginx can't proxy to backend in separate service
- Direct connection to API domain is required

---

## Console Warnings Resolved

**Before:**
- ❌ WebSocket connection failures
- ❌ CSP font violations (2 errors)
- ⚠️ 1Password icon 404 (browser extension)
- ⚠️ COOP postMessage (security working correctly)

**After:**
- ✅ WebSocket working
- ✅ Font CSP fixed (deploying with commit 0b82baf)
- ⚠️ 1Password 404 (harmless, browser extension)
- ⚠️ COOP postMessage (informational, security feature)

The two remaining warnings are external/expected and don't affect functionality.

---

## Recommendations

### Immediate
- ✅ **DONE** - WebSocket infrastructure working
- ✅ **DONE** - Docker permissions for debugging
- ✅ **DONE** - Console cleaned up

### Future Improvements
1. **Document Caddy override** in infrastructure docs (`~/git/mytech/`)
2. **Monitor beacon endpoint** for frontend errors:
   ```bash
   curl -sH "Authorization: Bearer $TOKEN" https://api.swarmlet.com/api/ops/errors | jq
   ```
3. **Consider server rename** (zerg → hatchery) to eliminate name collision
4. **Add to runbook** - "How to fix WebSocket if server is rebuilt"

---

## Technical Reference

### Key Files Modified
- `apps/zerg/frontend-web/public/config.js` - WebSocket URL routing
- `apps/zerg/frontend-web/nginx.conf` - CSP font sources
- `/data/coolify/proxy/caddy/dynamic/api-swarmlet-websocket.caddy` - Caddy override (manual)

### Key Commits
- `cb1680a` - Frontend WebSocket URL fix
- `0b82baf` - CSP font sources

### Infrastructure Details
- **Server**: zerg (Hetzner VPS)
- **Tailscale**: 100.120.197.80
- **Container Network**: mosksc0ogk0cssokckw0c8sc
- **Backend Service**: Uses hostname `backend` (stable across deploys)
- **Current Container**: backend-mosksc0ogk0cssokckw0c8sc-155704482261

### Health Monitoring
```bash
# Check WebSocket availability
curl -s https://api.swarmlet.com/api/system/health | jq '.ws'

# Check active connections
ssh zerg 'sg docker -c "docker logs backend-* --tail 20"'

# Check frontend errors
curl -sH "Authorization: Bearer $TOKEN" https://api.swarmlet.com/api/ops/errors | jq
```

---

## Appendix: Alternative Approaches Considered

### ❌ Approach 1: Override Docker Labels in Source
**Tried**: Adding Caddy labels to `docker-compose.prod.yml`
**Result**: Labels merged instead of overriding
**Why Failed**: Coolify appends rather than replaces labels

### ❌ Approach 2: Configure via Coolify UI
**Not Attempted**: No UI option to disable `try_files` for specific services
**Why Not Viable**: Coolify v4.0.0-beta.448 doesn't expose this level of control

### ✅ Approach 3: Manual Caddy Override File
**Implemented**: Created static config file in `/dynamic/`
**Why Successful**:
- Caddy detects ambiguous site definition
- Removes Docker label config automatically
- Our clean config takes precedence
- Persists through redeployments

---

## Sign-Off

**WebSocket infrastructure is now production-ready and robust.**

- Real-time features operational
- Console warnings minimal
- Solution survives redeployments
- Debugging access enabled

No further action required unless server is rebuilt from scratch (then reapply manual Caddy file).

---

*Report generated: 2025-11-27*
*Session: WebSocket debugging and infrastructure hardening*
