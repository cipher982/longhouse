#!/bin/bash
# Full Docker development with graceful shutdown and proper isolation
set -euo pipefail

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

# Central compose file reference - use unified compose with profile
export COMPOSE_FILE="docker/docker-compose.yml"
export COMPOSE_PROFILE="full"
STARTED=false
LOGS_PID=""
CLEANUP_DONE=false

# Helper for compose commands with profile
compose_cmd() {
    docker compose -f "$COMPOSE_FILE" --profile "$COMPOSE_PROFILE" "$@"
}

cleanup() {
    # Preserve exit status FIRST (before any other operations)
    local exit_status=$?

    # Guard against double cleanup (EXIT trap can race with INT)
    if [ "$CLEANUP_DONE" = true ]; then
        return
    fi
    CLEANUP_DONE=true

    echo ""
    echo -e "${YELLOW}🛑 Shutting down all services...${NC}"

    # Kill logs process if running
    if [ -n "$LOGS_PID" ] && kill -0 "$LOGS_PID" 2>/dev/null; then
        kill "$LOGS_PID" 2>/dev/null || true
        wait "$LOGS_PID" 2>/dev/null || true
    fi

    if [ "$STARTED" = true ]; then
        echo -e "${BLUE}  Stopping containers...${NC}"
        compose_cmd down || true
        echo -e "${GREEN}✅ All services stopped${NC}"
    fi

    exit "$exit_status"
}

# Trap EXIT catches normal exit, errors, and external stop
# INT/TERM for Ctrl+C and kill signals
trap cleanup EXIT INT TERM

echo -e "${BLUE}🚀 Starting unified development environment...${NC}"
echo -e "${BLUE}   (Full Docker with Nginx proxy - isolated ports)${NC}"
echo ""

# Load .env if present (respects user customizations)
if [ -f .env ]; then
    echo -e "${BLUE}📄 Loading .env file...${NC}"
    set -a
    source .env
    set +a
fi

# Set defaults only if not already set (respects existing env/exports)
export JARPXY_PORT="${JARPXY_PORT:-30080}"
export JARVIS_WEB_PORT="${JARVIS_WEB_PORT:-8080}"
export JARVIS_SERVER_PORT="${JARVIS_SERVER_PORT:-8787}"
export ZERG_BACKEND_PORT="${ZERG_BACKEND_PORT:-8000}"
export ZERG_FRONTEND_PORT="${ZERG_FRONTEND_PORT:-5173}"
export POSTGRES_DB="${POSTGRES_DB:-zerg}"
export POSTGRES_USER="${POSTGRES_USER:-zerg}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-zerg}"
export AUTH_DISABLED="${AUTH_DISABLED:-1}"
export DEV_ADMIN="${DEV_ADMIN:-1}"
# OPENAI_API_KEY must come from env or .env - no default

# Start in background
echo -e "${BLUE}📦 Starting containers (profile: $COMPOSE_PROFILE)...${NC}"
compose_cmd up -d --build
STARTED=true

# Wait for health checks
echo -e "${YELLOW}⏳ Waiting for services to be healthy...${NC}"
sleep 5

# Show status
compose_cmd ps

echo ""
echo -e "${GREEN}✅ Development environment ready!${NC}"
echo -e "${BLUE}   App:        http://localhost:${JARPXY_PORT}${NC}"
echo -e "${BLUE}   Chat:       http://localhost:${JARPXY_PORT}/chat${NC}"
echo -e "${BLUE}   Dashboard:  http://localhost:${JARPXY_PORT}/dashboard${NC}"
echo ""
echo -e "${YELLOW}Press Ctrl+C to stop everything${NC}"
echo ""

# Follow logs in background so Ctrl+C hits our trap first
compose_cmd logs -f &
LOGS_PID=$!

# Wait for logs process (keeps script alive)
wait "$LOGS_PID"
