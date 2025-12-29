# Swarm Platform Deployment Guide

**Version**: 2.2
**Date**: December 2025
**Status**: Production Ready (Durable Runs supported)

## Overview

This guide covers deploying the Swarm Platform (Jarvis + Zerg) to production environments.
V2.2 introduces **Durable Runs**, allowing executions to survive client disconnects and timeouts.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Production Setup                      │
├─────────────────────────────────────────────────────────┤
│                                                          │
│   [Reverse Proxy]  ←→  [Unified SPA]  ←→  [Zerg Backend] │
│   (Coolify/Caddy)       (React: /, /chat)   (FastAPI)    │
│                                                │         │
│                                          [PostgreSQL]    │
│                                                │         │
└─────────────────────────────────────────────────────────┘
```

## Prerequisites

### System Requirements

- **OS**: Linux (Ubuntu 22.04+ recommended)
- **Docker**: Docker Engine + `docker compose` plugin (recommended deployment path)
- **Database**: PostgreSQL 14+

### Required Services

- PostgreSQL database
- Reverse proxy / TLS termination (handled by Coolify in the standard setup)

## Deployment Options

### Option 1: Docker Compose (Recommended)

The simplest deployment uses the production compose file (and in the standard setup, Coolify runs it):

```bash
# Local development (full platform with nginx at port 30080)
make dev

# Production (hardened)
docker compose -f docker/docker-compose.prod.yml up -d
```

| Compose file | Services | Use Case |
|------------|----------|----------|
| `docker/docker-compose.dev.yml` (profile `dev`) | postgres, backend, frontend, reverse-proxy, dev-runner | Local dev via nginx at 30080 |
| `docker/docker-compose.prod.yml` | postgres, backend, frontend, reverse-proxy | Production deployment |

Deploy to Coolify:

1. Push code to git repository
2. Create new application in Coolify
3. Point to `docker/docker-compose.prod.yml`
4. Set environment variables in Coolify UI
5. Deploy

### Option 2: Manual Deployment (Legacy)

> **Note**: Manual deployment is rarely needed. Use Coolify (Option 1) for all
> production deployments. The sections below (Systemd Services, Nginx Configuration)
> provide reference configs if you need to run outside of Docker.

## Environment Configuration

### Critical Variables

#### Zerg Backend

```bash
# Database
DATABASE_URL="postgresql://zerg:password@localhost:5432/zerg_prod"

# OpenAI
OPENAI_API_KEY="sk-proj-..."

# Authentication
JWT_SECRET="<64-char-random-string>"
GOOGLE_CLIENT_ID="<client-id>.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET="<google-secret>"

# CORS (must include your public TLS origin)
# Comma-separated list. Example: "https://swarm.example.com,https://dashboard.swarm.example.com"
ALLOWED_CORS_ORIGINS="https://your-domain.com"

# Jarvis Integration
JARVIS_DEVICE_SECRET="<32-char-random-string>"

# Security
FERNET_SECRET="<32-byte-base64-encoded-key>"
TRIGGER_SIGNING_SECRET="<32-char-random-string>"

# Admin
ADMIN_EMAILS="your-email@example.com"
MAX_USERS="100"

# Limits
DAILY_RUNS_PER_USER="50"
DAILY_COST_PER_USER_CENTS="100"  # $1.00/day per user
DAILY_COST_GLOBAL_CENTS="10000"  # $100/day total

# Discord Notifications (optional)
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
DISCORD_ENABLE_ALERTS="1"

# Environment
ENVIRONMENT="production"
AUTH_DISABLED="0"
```

#### Frontend (Unified SPA)

```bash
# Same-origin API (recommended) vs explicit API origin (if you deploy split domains)
VITE_API_BASE_URL="/api"

# WebSocket base (dev uses ws://, prod should be wss:// behind TLS)
# VITE_WS_BASE_URL="wss://swarmlet.com/ws"

# Frontend auth guard (should match AUTH_DISABLED inverse)
VITE_AUTH_ENABLED="true"

# Analytics (optional)
VITE_UMAMI_WEBSITE_ID="..."
VITE_UMAMI_SCRIPT_SRC="https://analytics.drose.io/script.js"
VITE_UMAMI_DOMAINS="swarmlet.com"
```

### Generating Secrets

```bash
# JWT Secret (64 chars)
openssl rand -hex 32

# FERNET_SECRET (32 bytes, base64)
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Device Secret (32 chars)
openssl rand -hex 16

# Trigger Signing Secret
openssl rand -hex 16
```

## Database Setup

### PostgreSQL (Production)

```bash
# 1. Create database and user
sudo -u postgres psql << EOF
CREATE DATABASE zerg_prod;
CREATE USER zerg WITH PASSWORD 'secure-password';
GRANT ALL PRIVILEGES ON DATABASE zerg_prod TO zerg;
\c zerg_prod
GRANT ALL ON SCHEMA public TO zerg;
EOF

# 2. Run migrations
cd /opt/swarm/apps/zerg/backend
DATABASE_URL="postgresql://zerg:secure-password@localhost:5432/zerg_prod" \
  uv run alembic upgrade head

# 3. Verify
psql -U zerg -d zerg_prod -c "\dt"
# Should show: users, agents, agent_runs, agent_threads, etc.
```

### SQLite (Development/Small Deployments)

```bash
# 1. Use default DATABASE_URL
DATABASE_URL="sqlite:///./swarm.db"

# 2. Run migrations
cd apps/zerg/backend
uv run alembic upgrade head

# 3. Verify
sqlite3 swarm.db ".schema agent_runs"
```

## Systemd Services

### Backend Service

Create `/etc/systemd/system/zerg-backend.service`:

```ini
[Unit]
Description=Zerg Backend (Swarm Platform)
After=network.target postgresql.service

[Service]
Type=simple
User=swarm
Group=swarm
WorkingDirectory=/opt/swarm/apps/zerg/backend
EnvironmentFile=/opt/swarm/.env
ExecStart=/usr/local/bin/uv run uvicorn zerg.main:app --host 0.0.0.0 --port 47300
Restart=always
RestartSec=10

# Logging
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable zerg-backend
sudo systemctl start zerg-backend
sudo systemctl status zerg-backend
```

## Nginx Configuration

If you're deploying via Docker (recommended), use the repo-managed configs:

- Dev: `docker/nginx/docker-compose.unified.conf` (reverse-proxy container)
- Prod: `docker/nginx/nginx.prod.conf` (reverse-proxy container; baked into `docker/nginx.dockerfile`)

If you're running Nginx outside Docker, the rule is the same:

- Serve the SPA on `/` (includes `/chat`)
- Proxy `/api/` (and `/ws`) to the backend
- Disable buffering + set long timeouts for SSE endpoints under `/api/stream/` and streaming routes

## Health Checks

### Backend Health

```bash
# Reverse proxy health (fast)
curl https://swarmlet.com/health

# Backend readiness (includes DB check)
curl https://swarmlet.com/api/system/health

# Expected response:
# {"status":"ok","db":{"status":"ok"},...}
```

### SSE Stream

SSE endpoints are authenticated and tied to an active run. The easiest verification is the UI:

- Open `https://swarmlet.com/chat`
- Send a message that triggers workers/tools
- Confirm progress events stream without nginx buffering/timeouts

### Production Smoke Tests

After deployment, run the smoke test script to validate all critical endpoints:

```bash
# Test production
./scripts/smoke-prod.sh

# Wait 90s after deploy, then test
./scripts/smoke-prod.sh --wait

# Test custom URL
BASE_URL=https://staging.swarmlet.com ./scripts/smoke-prod.sh
```

The smoke test validates:

- Landing page (GET /)
- Health endpoint (GET /health)
- Dashboard auth redirect (GET /dashboard)
- Funnel batch origin check (POST /api/funnel/batch → 403 without Origin header)
- Auth verify returns 401 without session (GET /api/auth/verify)
- API proxy works (GET /api/users/me → 401)

## Monitoring

### Logs

```bash
# Backend logs
sudo journalctl -u zerg-backend -f

# Nginx logs
sudo tail -f /var/log/nginx/access.log
sudo tail -f /var/log/nginx/error.log

# Database logs
sudo journalctl -u postgresql -f
```

### Metrics

The backend exposes Prometheus metrics at `/metrics`.

In Docker production, `/metrics` is not proxied by default (it’s on the internal backend service). Options:

- Run it from inside the compose network (recommended):

```bash
docker compose -f docker/docker-compose.prod.yml exec -T backend curl -s http://127.0.0.1:8000/metrics | head
```

If you want `/metrics` publicly reachable, add an explicit nginx route (and protect it).

Key metrics:

- `agent_runs_total` - Total agent executions
- `agent_runs_duration_seconds` - Execution time
- `agent_runs_cost_usd` - Cost per run
- `websocket_connections` - Active connections

### Alerts

Configure Discord webhooks for budget alerts:

```bash
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
DISCORD_ENABLE_ALERTS="1"
```

## Backup & Recovery

### Database Backups

```bash
# Daily PostgreSQL backup
pg_dump -U zerg zerg_prod | gzip > backup_$(date +%Y%m%d).sql.gz

# Automated via cron
0 2 * * * pg_dump -U zerg zerg_prod | gzip > /backups/zerg_$(date +\%Y\%m\%d).sql.gz
```

### Restore from Backup

```bash
# 1. Drop existing database
sudo -u postgres psql -c "DROP DATABASE zerg_prod;"
sudo -u postgres psql -c "CREATE DATABASE zerg_prod OWNER zerg;"

# 2. Restore from backup
gunzip -c backup_20251006.sql.gz | psql -U zerg -d zerg_prod

# 3. Run any pending migrations
cd /opt/swarm/apps/zerg/backend
uv run alembic upgrade head
```

## Security Hardening

### 1. Firewall Rules

```bash
# Allow only necessary ports
sudo ufw allow 22/tcp      # SSH
sudo ufw allow 80/tcp      # HTTP
sudo ufw allow 443/tcp     # HTTPS
sudo ufw enable

# Block direct backend access
sudo ufw deny 47300/tcp
```

### 2. SSL/TLS

Use Let's Encrypt for free SSL certificates:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d jarvis.yourdomain.com
sudo certbot --nginx -d api.yourdomain.com
```

### 3. Database Security

```bash
# Restrict PostgreSQL to localhost
sudo nano /etc/postgresql/15/main/postgresql.conf
# Set: listen_addresses = 'localhost'

# Restart
sudo systemctl restart postgresql
```

### 4. Environment Secrets

```bash
# Protect .env file
sudo chown swarm:swarm /opt/swarm/.env
sudo chmod 600 /opt/swarm/.env

# Never commit secrets to git (ensure `.env` is ignored in your repo)
```

### 5. Rate Limiting

Configure in Zerg backend:

```bash
DAILY_RUNS_PER_USER="50"
DAILY_COST_PER_USER_CENTS="100"
DAILY_COST_GLOBAL_CENTS="10000"
```

## Scaling

### Horizontal Scaling

Run multiple backend instances behind a load balancer:

```nginx
upstream zerg_backend {
    server backend1:8000;
    server backend2:8000;
    server backend3:8000;
}

server {
    location /api/ {
        proxy_pass http://zerg_backend;
    }
}
```

**Note**: Requires shared PostgreSQL. WebSocket/SSE fanout is in-memory today; multi-instance WS requires either sticky routing or a shared broker.

### Database Scaling

For high load:

1. Use PostgreSQL connection pooling (pgBouncer)
2. Add read replicas for high-traffic read endpoints
3. Consider app-level caching only after measuring (avoid adding Redis by default)

### Background Workers
Separate long-running jobs into dedicated processes only if/when needed (start with the unified backend service).

## Troubleshooting

### Backend won't start

```bash
# Check logs
sudo journalctl -u zerg-backend -n 50

# Common issues:
# 1. Missing environment variables
cat /opt/swarm/.env | grep -E "OPENAI_API_KEY|DATABASE_URL|JWT_SECRET"

# 2. Database connection
psql -U zerg -d zerg_prod -c "SELECT 1;"

# 3. Port conflicts
sudo lsof -i:47300
```

### SSE connections timing out

```nginx
# Increase nginx timeouts
proxy_read_timeout 24h;
proxy_send_timeout 24h;
proxy_buffering off;
```

### Database migrations failing

```bash
# Check current migration version
cd /opt/swarm/apps/zerg/backend
uv run alembic current

# View migration history
uv run alembic history

# Force upgrade
uv run alembic upgrade head

# If stuck, check for locks
psql -U zerg -d zerg_prod -c "SELECT * FROM pg_locks;"
```

### High memory usage

```bash
# Check running processes
ps aux | grep "uvicorn\|python"

# Limit worker processes
# In systemd service:
Environment="WEB_CONCURRENCY=2"
```

## Maintenance

### Updating the Platform

```bash
# 1. Backup database
pg_dump -U zerg zerg_prod | gzip > backup_pre_update.sql.gz

# 2. Pull latest code
cd /opt/swarm
git pull origin main

# 3. Update dependencies
cd apps/zerg/backend && uv sync
cd /opt/swarm && bun install

# 4. Run migrations
cd apps/zerg/backend && uv run alembic upgrade head

# 5. Restart services
sudo systemctl restart zerg-backend

# 6. Verify health
curl https://swarmlet.com/health
```

### Database Maintenance

```bash
# Vacuum PostgreSQL (monthly)
psql -U zerg -d zerg_prod -c "VACUUM ANALYZE;"

# Check database size
psql -U zerg -d zerg_prod -c "\l+"

# Archive old runs (older than 90 days)
psql -U zerg -d zerg_prod << EOF
DELETE FROM agent_runs
WHERE created_at < NOW() - INTERVAL '90 days';
EOF
```

## Production Checklist

Before going live:

### Configuration

- [ ] All required environment variables set
- [ ] Strong secrets generated (64+ chars)
- [ ] Database configured with backups
- [ ] SSL certificates installed
- [ ] CORS origins configured properly

### Security

- [ ] Firewall rules in place
- [ ] Rate limiting enabled
- [ ] Cost budgets configured
- [ ] `.env` file permissions (600)
- [ ] Database connections encrypted

### Monitoring

- [ ] Health checks configured
- [ ] Prometheus metrics exposed
- [ ] Discord alerts enabled
- [ ] Log aggregation set up
- [ ] Uptime monitoring active

### Testing

- [ ] Run smoke tests: `./scripts/smoke-prod.sh`
- [ ] Test Jarvis authentication
- [ ] Verify SSE streaming works
- [ ] Test agent dispatch
- [ ] Confirm scheduled agents run

### Deployment

- [ ] Database migrations applied
- [ ] Baseline agents seeded
- [ ] Systemd services enabled
- [ ] Nginx configuration tested
- [ ] DNS records configured

## Support

For issues or questions:

- Check logs: `sudo journalctl -u zerg-backend -f`
- Review documentation: `docs/completed/jarvis_integration.md`
- Run smoke tests: `./scripts/smoke-prod.sh`

## Rollback Procedure

If deployment fails:

```bash
# 1. Stop services
sudo systemctl stop zerg-backend

# 2. Restore database
gunzip -c backup_pre_update.sql.gz | psql -U zerg -d zerg_prod

# 3. Revert code
git reset --hard <previous-commit>

# 4. Restart services
sudo systemctl start zerg-backend

# 5. Verify
curl https://swarmlet.com/health
```

## Performance Tuning

### Database Indexes

Ensure critical queries are fast:

```sql
-- Already created by migrations, verify:
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent_id ON agent_runs(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_created_at ON agent_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status);
```

### Connection Pooling

```python
# apps/zerg/backend/zerg/database.py
engine = create_engine(
    DATABASE_URL,
    pool_size=20,
    max_overflow=40,
    pool_pre_ping=True,
)
```

### Caching

Avoid adding Redis by default. Add caching only after measuring real hotspots.

## Next Steps

After deployment:

1. Validate endpoints with `./scripts/smoke-prod.sh`
2. Seed agents with `make seed-agents` (idempotent)
3. Review logs and error rates
4. Ensure backups are configured
5. Document any custom configuration

For ongoing development:

- See `docs/completed/jarvis_integration.md` for Jarvis API details
- Tools are defined in backend code (built-ins + allowlists); Jarvis uses `/api/jarvis/bootstrap` as the authoritative tool list
