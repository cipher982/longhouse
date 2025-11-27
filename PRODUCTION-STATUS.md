# Production Status & Pending Work
**Last Updated**: 2025-11-27
**Session**: WebSocket fix and infrastructure improvements

---

## 🚨 Current Production Issues

None - all known issues resolved!

---

## ✅ Fixes Completed This Session

### WebSocket 403 Forbidden (FIXED - 2025-11-27)
**Commits**: `cb1680a` - Frontend config fix
**Manual Fix**: Caddy override file on zerg server

**Problem**:
- WebSocket connections failing with 403 Forbidden
- Frontend connecting to wrong URL (wss://swarmlet.com instead of wss://api.swarmlet.com)
- Coolify's auto-generated Caddy labels included `try_files` which breaks WebSocket upgrades

**Root Cause**:
1. Coolify deploys frontend/backend as separate containers (nginx can't proxy "backend" hostname)
2. Coolify adds `try_files` labels to ALL services (meant for SPAs but breaks APIs)
3. Labels merge instead of override, causing conflicting Caddy config

**Solution**:
1. **Frontend**: Updated `config.js` to connect directly to `wss://api.swarmlet.com`
2. **Caddy Override**: Created `/data/coolify/proxy/caddy/dynamic/api-swarmlet-websocket.caddy`
   ```caddy
   https://api.swarmlet.com {
       encode zstd gzip
       header -Server
       reverse_proxy backend:8000
   }
   ```

**Why This is Robust**:
- ✅ Manual Caddy file persists through redeployments
- ✅ Uses stable hostname `backend` (Docker Compose service name)
- ✅ Overrides Docker labels when ambiguous site detected
- ✅ Clean config (no try_files, no handle_path)
- ✅ Works for both HTTP and WebSocket

**Testing**:
- HTTP: `curl https://api.swarmlet.com/api/system/health` → 200 OK
- WebSocket: Requests reach backend correctly (403 is expected for invalid tokens)
- Backend logs confirm: `('172.19.0.2', 41400) - "WebSocket /api/ws?token=test" 403`

### Docker Permissions (FIXED - 2025-11-27)
**Issue**: `zerg` user couldn't access Docker logs for debugging
**Fix**: Added zerg user to docker group: `sudo usermod -aG docker zerg`
**Result**: Can now debug production issues via SSH

---

## ✅ Previous Session Fixes (2025-11-25)

### Build & Deployment (FIXED)
**Commit**: `e1bc30b` - fix(docker): remove frozen-lockfile flag
- **Issue**: Frontend build failed - couldn't find bun.lock
- **Cause**: Dockerfile used `--frozen-lockfile` but lockfile is at repo root
- **Fix**: Removed `--frozen-lockfile` flag from Dockerfile

**Commit**: `b90cc5e` - fix(frontend): add @types/react-syntax-highlighter
- **Issue**: TypeScript build failing in production
- **Cause**: Missing type definitions for react-syntax-highlighter
- **Fix**: Added `@types/react-syntax-highlighter` to devDependencies

### CSP Meta Tag (FIXED)
**Commit**: `adfb059` - fix(frontend): remove hardcoded CSP meta tag
- **Issue**: HTML had hardcoded CSP with localhost entries
- **Cause**: Development CSP was baked into production build
- **Fix**: Removed meta tag, nginx handles CSP in production

### Database Connection (FIXED)
**Change**: Updated `DATABASE_URL` in Coolify environment variables
- **Issue**: Backend couldn't connect to database
- **Cause**: `DATABASE_URL=postgresql://...@localhost:5432/zerg` (wrong host)
- **Fix**: Changed to `DATABASE_URL=postgresql://...@postgres:5432/zerg`

---

## 🔧 Infrastructure Issues Discovered

### Docker Permissions (RESOLVED)
**Problem**: ~~Can't access backend logs to debug 500 error~~ FIXED

**Resolution** (2025-11-27):
- Added `zerg` user to docker group
- Can now access Docker logs via SSH
- Enabled direct debugging of production issues

### Naming Confusion
**Problem**: Server name "zerg" collides with project name "zerg"
- Makes communication confusing ("zerg server" vs "zerg project")
- Other servers use unique names (clifford, cube, slim, cinder)

**Proposed Solution**: Rename server to "hatchery"
- Fits StarCraft/Zerg theme
- Eliminates confusion with project codebase
- See detailed rename checklist below

---

## 📋 Server Rename Plan: zerg → hatchery

### Why Rename?
- Eliminate confusion between "zerg server" and "zerg project"
- Easier to discuss infrastructure vs application issues
- Matches naming pattern of other servers

### What Needs Updating:

#### Local Machine (cinder)
- [ ] `~/.ssh/config` - Change `Host zerg` to `Host hatchery`
- [ ] `~/git/mytech/infrastructure/vps.md` - Update documentation
- [ ] `~/git/mytech/infrastructure/overview.md` - Update server list
- [ ] `~/git/mytech/CLAUDE.md` - Update server references

#### Server (100.120.197.80)
- [ ] `/etc/hostname` - Change to `hatchery`
- [ ] `/etc/hosts` - Update `127.0.1.1` entry
- [ ] `sudo hostnamectl set-hostname hatchery`
- [ ] **REBOOT** - Required for hostname change to fully propagate

#### External Systems
- [ ] **Tailscale** - Rename device in admin console (https://login.tailscale.com/admin/machines)
- [ ] **Hetzner** - Rename server label (cosmetic, optional)

#### What Stays The Same
- ✅ Username: `zerg` (no need to rename user)
- ✅ Tailscale IP: 100.120.197.80
- ✅ SSH key: rosetta (unchanged)
- ✅ Project code: "zerg" in repo stays as-is

### While We're At It
**Add docker permissions**:
```bash
ssh zerg
sudo usermod -aG docker zerg
# Logout and back in for group to take effect
```

This solves the "can't check Docker logs" problem.

---

## 🔍 Next Steps

### Optional: Infrastructure Improvements
1. **Rename server** (follow checklist above) - Eliminate "zerg" name collision
2. **Monitor beacon errors**: `curl -sH "Authorization: Bearer $TOKEN" https://api.swarmlet.com/api/ops/errors | jq`
3. **Test workflows end-to-end** - Verify WebSocket real-time updates work in production

---

## 🔗 Related Sessions

**Previous work**:
- Initial deployment setup (August 2025)
- Database migration (DATE UNKNOWN)
- Frontend build fixes (November 2025)

**Key repos**:
- Project: `~/git/zerg/` (Swarmlet AI platform)
- Infrastructure: `~/git/mytech/` (server documentation)
- History: `~/git/obsidian_vault/` (session logs)

---

## 📝 Notes

**Security Context**:
- Server was "locked down" for paid app security
- `zerg` user intentionally lacks docker permissions
- Trade-off: Security vs operational ease
- Consider: Is the security model working or hindering?

**Deployment Context**:
- Frontend: https://swarmlet.com (via Coolify/Caddy)
- Backend: https://api.swarmlet.com (via Coolify/Caddy)
- Database: PostgreSQL in Docker (postgres:5432)
- All managed via Coolify on server

**Key Files**:
- `~/git/zerg/CLAUDE.md` - Project-specific development guide
- `~/git/mytech/infrastructure/vps.md` - Server documentation
- Coolify env vars - Production configuration
