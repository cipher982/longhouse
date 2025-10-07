# Swarm Platform Deployment Guide

**Version**: 1.0
**Date**: October 6, 2025
**Status**: Production Ready (Backend), UI Integration Pending

## Overview

This guide covers deploying the Swarm Platform (Jarvis + Zerg) to production environments.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Production Setup                      │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  [Jarvis PWA]  ←→  [Nginx/Caddy]  ←→  [Zerg Backend]   │
│   (Static)           (Reverse            (FastAPI)      │
│                       Proxy)              + Workers      │
│                                                │         │
│                                          [PostgreSQL]    │
│                                                │         │
│                                          [Redis/Cache]   │
└─────────────────────────────────────────────────────────┘
```

## Prerequisites

### System Requirements
- **OS**: Linux (Ubuntu 22.04+ recommended)
- **Python**: 3.11+
- **Node.js**: 18+
- **Database**: PostgreSQL 14+ (or SQLite for small deployments)
- **Memory**: 2GB+ RAM
- **Storage**: 10GB+ disk space

### Required Services
- PostgreSQL database
- (Optional) Redis for caching
- (Optional) Nginx/Caddy for reverse proxy

## Deployment Options

### Option 1: Docker Compose (Recommended)

The simplest deployment uses Docker Compose with Coolify:

```yaml
# docker-compose.prod.yml (already exists in repo)
services:
  backend:
    build: ./apps/zerg/backend
    environment:
      - DATABASE_URL=postgresql://user:pass@db:5432/zerg
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - JARVIS_DEVICE_SECRET=${JARVIS_DEVICE_SECRET}
      - JWT_SECRET=${JWT_SECRET}
    ports:
      - "47300:47300"

  db:
    image: postgres:15-alpine
    volumes:
      - postgres_data:/var/lib/postgresql/data
```

Deploy to Coolify:
1. Push code to git repository
2. Create new application in Coolify
3. Point to `docker-compose.prod.yml`
4. Set environment variables in Coolify UI
5. Deploy

### Option 2: Manual Deployment

#### Backend (Zerg)

```bash
# 1. Clone repository on server
git clone <repo-url> /opt/swarm
cd /opt/swarm

# 2. Install Python dependencies
cd apps/zerg/backend
uv sync --frozen

# 3. Configure environment
cp .env.example /opt/swarm/.env
nano /opt/swarm/.env

# 4. Run migrations
uv run alembic upgrade head

# 5. Seed agents
uv run python scripts/seed_jarvis_agents.py

# 6. Start with systemd
sudo cp /opt/swarm/deploy/zerg-backend.service /etc/systemd/system/
sudo systemctl enable zerg-backend
sudo systemctl start zerg-backend
```

#### Frontend (Jarvis)

```bash
# 1. Build Jarvis PWA
cd /opt/swarm/apps/jarvis/apps/web
npm install
npm run build

# 2. Serve static files with nginx
sudo cp /opt/swarm/deploy/nginx-jarvis.conf /etc/nginx/sites-available/jarvis
sudo ln -s /etc/nginx/sites-available/jarvis /etc/nginx/sites-enabled/
sudo systemctl reload nginx
```

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

# Jarvis Integration
JARVIS_DEVICE_SECRET="<32-char-random-string>"

# Security
FERNET_SECRET="<32-byte-base64-encoded-key>"
TRIGGER_SIGNING_SECRET="<32-char-random-string>"

# Admin
ADMIN_EMAILS="your-email@example.com"
MAX_USERS="100"

# Limits
MAX_OUTPUT_TOKENS="2000"
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

#### Jarvis Frontend
```bash
# Zerg Backend URL
VITE_ZERG_API_URL="https://api.swarmlet.com"

# Device Secret (must match backend)
VITE_JARVIS_DEVICE_SECRET="<same-as-backend-secret>"

# OpenAI for local voice processing
OPENAI_API_KEY="sk-..."
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

### Jarvis Node Server (Optional)

If using Jarvis with MCP bridge:

Create `/etc/systemd/system/jarvis-server.service`:

```ini
[Unit]
Description=Jarvis Node Server
After=network.target

[Service]
Type=simple
User=swarm
Group=swarm
WorkingDirectory=/opt/swarm/apps/jarvis/apps/server
EnvironmentFile=/opt/swarm/.env
ExecStart=/usr/bin/node server.js
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Nginx Configuration

### Jarvis PWA

Create `/etc/nginx/sites-available/jarvis`:

```nginx
server {
    listen 80;
    server_name jarvis.yourdomain.com;

    # Redirect to HTTPS
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name jarvis.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/jarvis.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/jarvis.yourdomain.com/privkey.pem;

    # PWA static files
    root /opt/swarm/apps/jarvis/apps/web/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    # API proxy to Zerg backend
    location /api/ {
        proxy_pass http://localhost:47300;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE specific
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 24h;
    }
}
```

### Zerg API (Optional - direct access)

Create `/etc/nginx/sites-available/zerg-api`:

```nginx
server {
    listen 443 ssl http2;
    server_name api.swarmlet.com;

    ssl_certificate /etc/letsencrypt/live/api.swarmlet.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.swarmlet.com/privkey.pem;

    location / {
        proxy_pass http://localhost:47300;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_buffering off;
        proxy_read_timeout 24h;
    }
}
```

## Health Checks

### Backend Health

```bash
curl https://api.swarmlet.com/api/health

# Expected response:
# {"status": "healthy", "version": "1.0.0"}
```

### Database Connectivity

```bash
curl https://api.swarmlet.com/api/health/db

# Expected response:
# {"status": "healthy", "database": "connected"}
```

### SSE Stream

```bash
# Authenticate (stores HttpOnly session cookie)
curl -s -X POST https://api.swarmlet.com/api/jarvis/auth \
  -H "Content-Type: application/json" \
  -d '{"device_secret":"<device-secret>"}' \
  -c cookies.txt -b cookies.txt

# Stream events using the stored session
curl -N https://api.swarmlet.com/api/jarvis/events \
  -b cookies.txt

# Should receive connected event immediately
```

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

The platform exposes Prometheus metrics at `/metrics`:

```bash
curl http://localhost:47300/metrics
```

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

# Never commit secrets to git
echo ".env" >> .gitignore
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
    server backend1:47300;
    server backend2:47300;
    server backend3:47300;
}

server {
    location /api/ {
        proxy_pass http://zerg_backend;
    }
}
```

**Note**: Requires shared PostgreSQL and Redis for state.

### Database Scaling

For high load:
1. Use PostgreSQL connection pooling (pgBouncer)
2. Add read replicas for `/api/jarvis/agents` and `/api/jarvis/runs`
3. Cache agent listings in Redis (60s TTL)

### Background Workers

Separate LangGraph execution into dedicated workers:

```bash
# Worker 1: Scheduled agents
uv run python -m zerg.services.scheduler_service

# Worker 2: On-demand dispatches
uv run python -m zerg.workers.dispatch_worker

# Worker 3: Email triggers
uv run python -m zerg.services.email_trigger_service
```

## Monitoring

### Uptime Monitoring

Add health check endpoints to your monitoring service:

```bash
# Backend health
GET https://api.swarmlet.com/api/health
# Expected: 200 OK

# Database health
GET https://api.swarmlet.com/api/health/db
# Expected: 200 OK
```

### Application Metrics

Integrate Prometheus:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'zerg-backend'
    static_configs:
      - targets: ['localhost:47300']
    metrics_path: '/metrics'
```

### Log Aggregation

Send logs to centralized service:

```bash
# Using Loki
sudo apt install promtail
sudo nano /etc/promtail/config.yml
# Configure to scrape journald logs
```

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
cd apps/jarvis && npm install

# 4. Run migrations
cd apps/zerg/backend && uv run alembic upgrade head

# 5. Restart services
sudo systemctl restart zerg-backend
sudo systemctl restart jarvis-server  # if applicable

# 6. Verify health
curl https://api.swarmlet.com/api/health
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
- [ ] Run integration tests: `./scripts/test-jarvis-integration.sh`
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
- Review documentation: `/docs/jarvis_integration.md`
- Test integration: `./scripts/test-jarvis-integration.sh`

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
curl https://api.swarmlet.com/api/health
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

Add Redis for agent listings:

```python
# Cache agent list for 60 seconds
@router.get("/api/jarvis/agents")
@cache(expire=60)
async def list_jarvis_agents(...):
    ...
```

## Next Steps

After deployment:
1. Test all endpoints with `./scripts/test-jarvis-integration.sh`
2. Seed agents with `make seed-jarvis-agents`
3. Monitor logs for first 24 hours
4. Set up automated backups
5. Configure monitoring alerts
6. Document any custom configuration

For ongoing development:
- See [Jarvis Integration](./jarvis_integration.md) for API details
- See [Tool Manifest Workflow](./tool_manifest_workflow.md) for adding tools
- See main README.swarm.md for development workflow
