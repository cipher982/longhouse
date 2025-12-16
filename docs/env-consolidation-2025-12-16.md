# .env Consolidation Summary - Zerg Monorepo

**Date:** 2025-12-16
**Status:** ✅ Complete

## Overview

All `.env` files in the Zerg monorepo have been consolidated into a single root-level `.env` file at:

```
~/git/zerg/.env
```

## Backups

All original `.env` files were backed up to:

```
~/git/zerg/.env-backups-2025-12-16/
```

Backup includes:

- `.env-backups-2025-12-16/.env` (root .env - original)
- `.env-backups-2025-12-16/apps/zerg/frontend-web/.env`
- `.env-backups-2025-12-16/apps/zerg/backend/.env`
- `.env-backups-2025-12-16/apps/jarvis/apps/web/.env`

**⚠️ DO NOT DELETE THE BACKUP DIRECTORY** - It contains the only copies of the original configuration.

## What Was Consolidated

### Root `.env` (already existed)

- OpenAI / LLM configuration
- Database configuration
- Authentication / Security
- Frontend build flags
- Development server ports
- Runtime environment
- Discord notifications
- Jarvis integration

### From `apps/zerg/frontend-web/.env`

- `VITE_PROXY_TARGET` - Proxy target for Vite dev server
- `VITE_API_BASE_URL` - API base URL for client-side requests
- `VITE_WS_BASE_URL` - WebSocket URL
- `VITE_UMAMI_*` - Umami analytics configuration
- `FRONTEND_PORT` - Frontend development port

### From `apps/zerg/backend/.env`

- `CONTAINER_*` - Container tools configuration for testing
- `ALLOWED_MODELS_NON_ADMIN` - Model restrictions
- `TMPDIR`, `XDG_CACHE_HOME` - UV cache directories
- `DOCKER_HOST` - Docker socket path

### From `apps/jarvis/apps/web/.env`

- `VITE_ZERG_API_URL` - Zerg backend URL for Jarvis
- `VITE_JARVIS_DEVICE_SECRET` - Device authentication secret
- `VITE_VOICE_CONTEXT` - Voice context setting
- Umami analytics configuration (shared with Zerg frontend)

## Configuration Changes Made

### 1. Updated Vite Configurations

#### `apps/zerg/frontend-web/vite.config.ts`

**Changed:**

```typescript
// Before:
const rootEnv = loadEnv(mode, __dirname, "");

// After:
const repoRoot = path.resolve(__dirname, "../../..");
const rootEnv = loadEnv(mode, repoRoot, "");
```

#### `apps/jarvis/apps/web/vite.config.ts`

**Changed:**

```typescript
// Before:
export default defineConfig({
  // ... config

// After:
export default defineConfig(({ mode }) => {
  const repoRoot = resolve(__dirname, '../../../..')
  loadEnv(mode, repoRoot, '')
  // ... config
  return {
    // ... config
  }
})
```

### 2. Backend Configuration (No Changes Needed)

The backend configuration in `apps/zerg/backend/zerg/config/__init__.py` already:

- Detects the repo root automatically
- Loads `.env` from repo root: `_REPO_ROOT / ".env"`
- Supports `.env.test` for test environments
- No changes were required

### 3. Docker Compose (No Changes Needed)

Docker Compose files (`docker/docker-compose.dev.yml`, `docker/docker-compose.prod.yml`):

- Already use environment variable substitution from shell
- No `env_file` directives present
- Variables are passed through from the shell environment
- Docker Compose automatically reads `.env` from the directory where `docker-compose` is run

### 4. Subdirectory `.env` Files

Replaced with notice files pointing to root `.env`:

- `apps/zerg/frontend-web/.env` - Notice file
- `apps/zerg/backend/.env` - Notice file
- `apps/jarvis/apps/web/.env` - Notice file

## Root `.env` Structure

The consolidated `.env` file is organized into 13 sections:

1. **OpenAI / LLM** - API keys and streaming settings
2. **LangSmith** - Optional tracing configuration
3. **Database** - PostgreSQL settings for Docker Compose
4. **Authentication / Security** - OAuth, JWT, Fernet encryption
5. **Frontend build flags** - Production API endpoints
6. **Development Server Ports** - Configurable ports for parallel projects
7. **Runtime** - Environment mode settings
8. **Discord Notifications** - Webhook alerts and digest
9. **Jarvis Integration** - Device secrets
10. **Database Connection** - Connection URL
11. **Frontend - Zerg Web** - Vite proxy, API URLs, analytics
12. **Frontend - Jarvis Web** - Jarvis-specific settings
13. **Backend Container Tools** - Testing configuration

## Verification Steps

### 1. Verify Backend Loads Environment Correctly

```bash
cd ~/git/zerg/apps/zerg/backend
uv run python -c "from zerg.config import get_settings; s = get_settings(); print(f'OpenAI Key: {s.openai_api_key[:20]}...'); print(f'Database: {s.database_url}'); print(f'Jarvis Secret: {s.jarvis_device_secret}')"
```

Expected output should show values from root `.env`:

- OpenAI key starts with `sk-proj-QgkOU4SEU6VFn...`
- Database URL: `postgresql://zerg:dev@localhost:5432/zerg`
- Jarvis secret: `test-secret-for-integration-testing-change-in-production`

### 2. Verify Zerg Frontend Loads Environment Correctly

```bash
cd ~/git/zerg/apps/zerg/frontend-web
bun run dev --port 47200
```

Then check the browser console or network tab to verify:

- API requests go to `/api` (proxy configured)
- WebSocket connects to `ws://localhost:47300/ws`
- Umami analytics script loads from `https://analytics.drose.io/script.js`

### 3. Verify Jarvis Web Loads Environment Correctly

```bash
cd ~/git/zerg/apps/jarvis/apps/web
bun run dev
```

Check that:

- VITE_ZERG_API_URL is available in the client
- VITE_VOICE_CONTEXT is set to "personal"

### 4. Verify Docker Compose Uses Root `.env`

```bash
cd ~/git/zerg/docker
docker compose --profile zerg up --build
```

Check that services start correctly and environment variables are passed through:

- Backend service has OPENAI_API_KEY
- Frontend service has VITE\_\* variables
- Database service has POSTGRES\_\* variables

### 5. Test Full Platform

```bash
cd ~/git/zerg
make dev  # or: make zerg
```

Verify:

- Backend API responds at `http://localhost:47300`
- Frontend loads at `http://localhost:47200`
- Authentication works (if enabled)
- LLM features work
- Database connections succeed

## Important Notes

### Environment Variable Precedence

1. **Shell environment** - Takes highest precedence
2. **Root `.env`** - Loaded by all applications
3. **Docker Compose substitution** - Uses shell environment and `.env`
4. **Vite client-side** - Only `VITE_*` prefixed variables are exposed

### Adding New Variables

To add new environment variables:

1. **For backend:** Add to `~/git/zerg/.env` (any section, preferably organized)
2. **For Zerg frontend:** Add with `VITE_` prefix in section 11
3. **For Jarvis frontend:** Add with `VITE_` prefix in section 12
4. **For Docker Compose:** Add to root `.env`, reference in `docker-compose.*.yml`

### Secrets Management

⚠️ **IMPORTANT:** The root `.env` file contains secrets:

- OpenAI API key
- Google OAuth credentials
- GitHub OAuth credentials
- JWT secret
- Fernet encryption key
- Discord webhook URL

Ensure `.env` is in `.gitignore` and never committed to version control.

### Testing Environments

The backend supports different environment modes via `ENVIRONMENT` variable:

- `development` - Uses root `.env`
- `test` - Looks for `.env.test` first, falls back to `.env`
- `production` - Uses root `.env` with production settings

## Rollback Instructions

If you need to rollback to the previous setup:

```bash
cd ~/git/zerg

# Restore original .env files from backup
cp .env-backups-2025-12-16/.env .env
cp .env-backups-2025-12-16/apps/zerg/frontend-web/.env apps/zerg/frontend-web/.env
cp .env-backups-2025-12-16/apps/zerg/backend/.env apps/zerg/backend/.env
cp .env-backups-2025-12-16/apps/jarvis/apps/web/.env apps/jarvis/apps/web/.env

# Revert vite.config.ts changes
git checkout apps/zerg/frontend-web/vite.config.ts
git checkout apps/jarvis/apps/web/vite.config.ts
```

## Git Status

The following files were modified (not committed):

- `.env` - Consolidated all variables
- `apps/zerg/frontend-web/.env` - Replaced with notice file
- `apps/zerg/backend/.env` - Replaced with notice file
- `apps/jarvis/apps/web/.env` - Replaced with notice file
- `apps/zerg/frontend-web/vite.config.ts` - Updated to load from repo root
- `apps/jarvis/apps/web/vite.config.ts` - Updated to load from repo root

The backup directory `.env-backups-2025-12-16/` should be added to `.gitignore`.

## Next Steps

1. **Test thoroughly** - Run all verification steps above
2. **Update documentation** - Update any README files that reference subdirectory .env files
3. **Update CI/CD** - Ensure deployment scripts reference root `.env`
4. **Team communication** - Notify team members of the change
5. **Consider committing** - Once verified working, commit the changes (excluding `.env` itself)

## Questions or Issues?

If you encounter issues:

1. Check the backup directory has all original files
2. Review this document for verification steps
3. Check vite.config.ts changes for syntax errors
4. Verify Docker Compose is run from repo root
5. Check backend config loading logic in `apps/zerg/backend/zerg/config/__init__.py`
