# Coolify Debugging Guide

## Quick Log Access

**Fetch latest deployment logs:**
```bash
./scripts/get-coolify-logs.sh [number]
```

This queries the Coolify database directly:
- Application ID: 30 (zerg)
- Stored on clifford → coolify-db container
- Full build output including all docker compose build stderr

## Why Coolify Debugging Is Hard

**The stack:**
```
You (laptop)
  → Coolify (PHP on clifford)
    → SSH to target server (zerg)
      → Docker helper container
        → docker compose build
          → Service containers
            → Build tools (bun/tsc/vite/uv)
```

**Common failure modes:**

| What Coolify Shows | Actual Problem | How to Diagnose |
|-------------------|----------------|-----------------|
| "Command failed with no error output (255)" | SSH session dropped | Check clifford: `docker logs coolify` |
| "RuntimeException at ExecuteRemoteCommand.php:243" | Coolify's exec wrapper crashed | Use `./scripts/get-coolify-logs.sh` |
| Truncated build output | Coolify UI pagination | Query database directly (script above) |
| "Variable not set" warnings | Env vars not in build-time.env | Check Coolify → App → Env → "Available at Build Time" |

## Debugging Workflow

### 1. Get Full Logs
```bash
./scripts/get-coolify-logs.sh 1 > /tmp/coolify-debug.log
grep -E "error|ERROR|fail|FAIL" /tmp/coolify-debug.log
```

### 2. Test Build Locally
```bash
# Same context as Coolify
docker build -f apps/zerg/frontend-web/Dockerfile --target production .

# Or full compose
docker compose -f docker/docker-compose.prod.yml build
```

### 3. Check What's In Git
```bash
# Verify files exist in repo
git ls-files apps/zerg/frontend-web/src/jarvis/data/

# Check .gitignore isn't excluding source
git check-ignore -v apps/zerg/frontend-web/src/jarvis/data/
```

### 4. Test On Target Server
```bash
ssh zerg
cd /tmp && git clone https://github.com/cipher982/zerg.git test-build
cd test-build
docker compose -f docker/docker-compose.prod.yml build frontend
```

## Common Gotchas

### Files Missing From Git
**Symptom:** "Cannot find module" errors only in Coolify
**Cause:** `.gitignore` excluding source code (e.g., `data/` instead of `/data/`)
**Fix:** Use leading `/` for root-level patterns only

### Docker Cache Hiding Issues
**Symptom:** Build works locally, fails in Coolify
**Cause:** Local Docker has stale cache with old files
**Fix:** Build with `--no-cache` to see real state

### Env Vars Not Available at Build Time
**Symptom:** "Variable not set" warnings, blank values in containers
**Cause:** Coolify env vars set as runtime-only
**Fix:** Coolify UI → Environment Variables → Check "Available at Build Time"

## When To Upgrade Server

Stay on CPX11/21 (2-4GB) if:
- Single service
- Small dependencies
- Infrequent builds

Upgrade to CPX31+ (8GB) when:
- Multi-service parallel builds
- Heavy dependencies (Python ML, large JS bundles)
- Frequent deployments (faster is worth it)

## Root Cause This Session

**The bug:** `.gitignore` had `data/` which excluded `apps/zerg/frontend-web/src/jarvis/data/*.ts`

**Why it was hard to find:**
1. Files existed locally (Docker cache worked)
2. Git clone on Coolify didn't get them
3. TypeScript errors only showed after cache invalidation
4. Coolify's SSH wrapper crashed before showing full error (exit 255)

**The fix:** Changed `data/` → `/data/` in `.gitignore`

**Everything else:** Unnecessary workarounds that should be cleaned up
