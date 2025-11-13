#!/bin/bash
set -e

echo "Starting Unified Dev Environment..."

# Export required ports (fail-fast configuration)
export JARPXY_PORT=30080
export ZGPXY_PORT=30081
export JARVIS_WEB_PORT=8080
export JARVIS_SERVER_PORT=8787
export ZERG_BACKEND_PORT=8000
export ZERG_FRONTEND_PORT=5173

# Also set some common Zerg environment variables for development
export POSTGRES_DB=zerg
export POSTGRES_USER=zerg
export POSTGRES_PASSWORD=zerg
export AUTH_DISABLED=1
export DEV_ADMIN=1
export OPENAI_API_KEY=${OPENAI_API_KEY:-}

echo "Ports configured:"
echo "  Jarvis PWA:     http://localhost:$JARPXY_PORT"
echo "  Zerg Dashboard: http://localhost:$ZGPXY_PORT"
echo ""
echo "Starting containers..."

docker compose -f docker-compose.unified.yml up --build -d

echo ""
echo "✅ Unified dev environment started!"
echo ""
echo "Access points:"
echo "  - Jarvis PWA:     http://localhost:$JARPXY_PORT"
echo "  - Zerg Dashboard: http://localhost:$ZGPXY_PORT"
echo ""
echo "To stop: docker compose -f docker-compose.unified.yml down"
