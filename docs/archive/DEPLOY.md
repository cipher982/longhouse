# 🚀 Deployment Guide - Coolify + Hetzner VPS

This guide covers deploying the Zerg AI Agent Platform using Docker Compose on Coolify.

## 📋 **Prerequisites**

- Hetzner VPS with Coolify installed
- Domain pointed to your VPS
- Coolify reverse proxy configured (Traefik)

## 🔧 **Local Testing (Optional)**

Test the full stack locally before deploying:

```bash
# 1. Build frontend for production (same-origin proxy enabled)
cd frontend && BUILD_ENV=production ./build-only.sh

# 2. Generate production secrets
python generate-production-secrets.py

# 3. Create test environment file
cp .env.production .env.local
# Edit .env.local with the generated secrets

# 4. Start the stack
docker-compose --env-file .env.local up --build

# 5. Test at http://localhost (nginx) -> backend at :8000
```

## 🌐 **Coolify Deployment**

### **Step 1: Create New Service**

1. In Coolify: **New Resource** → **Docker Compose**
2. **Repository**: Paste your git repository URL
3. **Branch**: `main`
4. **Docker Compose File**: `docker-compose.yml`

### **Step 2: Configure Environment Variables**

In Coolify environment settings, add these variables:

```bash
# Generate these first with: python generate-production-secrets.py
POSTGRES_PASSWORD=your_generated_password
JWT_SECRET=your_generated_jwt_secret
FERNET_SECRET=your_generated_fernet_key
TRIGGER_SIGNING_SECRET=your_generated_trigger_secret

# Your production domain
DOMAIN=your-domain.com
ALLOWED_CORS_ORIGINS=https://your-domain.com

# Google OAuth (create new credentials for production)
GOOGLE_CLIENT_ID=your_production_client_id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your_production_client_secret

# OpenAI (production key)
OPENAI_API_KEY=sk-your_production_openai_key

# Optional: LangSmith tracing
# LANGCHAIN_API_KEY=ls-your_langsmith_key
# LANGCHAIN_TRACING_V2=true
# LANGCHAIN_PROJECT=zerg-production
```

### **Step 3: Network Configuration**

- Coolify automatically creates the `web` network for Traefik
- No port exposure needed - reverse proxy handles everything
- Services communicate internally via Docker networks

### **Step 4: Deploy**

1. Click **Deploy** in Coolify
2. Monitor logs for successful startup
3. Check health at `https://your-domain.com/health`

## 🔍 **Post-Deployment Verification**

### **Health Checks**

- **Frontend**: `https://your-domain.com/health` → "healthy"
- **Backend API**: `https://your-domain.com/api/` → proxied by frontend Nginx
- **WebSocket**: Check browser console for successful WS connection

### **Database Connection**

- Check backend logs for successful PostgreSQL connection
- Tables are created automatically on first run

### **Authentication**

- Visit your domain and try Google Sign-In
- Verify JWT tokens are working properly

## 🛠️ **Troubleshooting**

### **Common Issues**

**1. Frontend 404 errors**

- Check that `frontend/www/` contains built WASM files
- Run `cd frontend && ./build-only.sh` before deployment

**2. Database connection errors**

- Verify `POSTGRES_PASSWORD` matches in both services
- Check PostgreSQL container logs for startup issues

**3. CORS errors**

- Production uses same-origin proxying; no CORS should be required. If you see
  CORS errors, verify frontend Nginx has `/api/` and `/api/ws` proxy rules and
  backend runs with `--proxy-headers`.

**4. Google OAuth issues**

- Update Google OAuth redirect URIs to include your production domain
- Verify `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`

### **Debugging Commands**

```bash
# View logs in Coolify
# Or via docker:
docker-compose logs backend
docker-compose logs postgres
docker-compose logs frontend

# Check container health
docker-compose ps

# Test internal connectivity
docker-compose exec backend curl http://postgres:5432
```

## 📊 **Monitoring & Maintenance**

### **Backup Strategy**

- PostgreSQL data is persisted in `postgres_data` volume
- Consider setting up automated database backups via Coolify
- Static files persisted in `backend_static` volume

### **Updates**

1. Push changes to your git repository
2. Coolify can auto-deploy on git push (configure webhook)
3. Or manually trigger deployment in Coolify UI

### **Scaling**

Current setup is single-instance. For scaling:

- Add Redis for distributed event bus
- Use external PostgreSQL (managed service)
- Load balance multiple backend instances

## 🔒 **Security Notes**

- All secrets generated with cryptographically secure methods
- No ports exposed directly - reverse proxy only
- Database isolated on internal network
- HTTPS enforced via Traefik/Let's Encrypt
- Non-root user in backend container

## 📞 **Support**

If deployment fails:

1. Check Coolify deployment logs
2. Verify all environment variables are set
3. Ensure domain DNS points to your VPS
4. Test local Docker Compose setup first
