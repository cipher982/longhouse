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
export COMPOSE_FILE="docker/docker-compose.dev.yml"
export COMPOSE_PROFILE="full"
export COMPOSE_PROJECT_NAME="zerg"
STARTED=false
LOGS_PID=""
CLEANUP_DONE=false

# Helper for compose commands with profile.
# Important: compose file lives in `docker/`, but `.env` lives at repo root.
# `--env-file` makes env interpolation deterministic regardless of CWD.
compose_cmd() {
    docker compose --project-name "$COMPOSE_PROJECT_NAME" --env-file .env -f "$COMPOSE_FILE" --profile "$COMPOSE_PROFILE" "$@"
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

# Require .env (fail fast)
if [ ! -f .env ]; then
    echo -e "${RED}❌ Missing .env file${NC}"
    echo -e "${YELLOW}💡 Copy .env.example to .env and fill in required values.${NC}"
    exit 1
fi

echo -e "${BLUE}📄 Loading .env file...${NC}"
set -a
source .env
set +a

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

# Validate required env vars (fail fast, don't start half-configured stacks)
missing=0
for var in POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB; do
    if [ -z "${!var:-}" ]; then
        echo -e "${RED}❌ Missing required env var: ${var}${NC}"
        missing=1
    fi
done
if [ "$missing" = "1" ]; then
    exit 1
fi

# Warn if a different Postgres container is already running.
# This avoids confusing "why am I connected to the wrong DB?" moments.
OTHER_POSTGRES_CONTAINERS=$(docker ps --format '{{.Names}}\t{{.Image}}\t{{.Labels}}' \
    | awk '$2 ~ /^postgres:/ {print $0}' \
    | grep -v "com.docker.compose.project=${COMPOSE_PROJECT_NAME}" || true)
if [ -n "$OTHER_POSTGRES_CONTAINERS" ]; then
    echo -e "${YELLOW}⚠️  Detected other running Postgres containers (outside compose project: ${COMPOSE_PROJECT_NAME})${NC}"
    echo "$OTHER_POSTGRES_CONTAINERS" | sed 's/^/   - /'
    echo -e "${YELLOW}   This can cause confusing DB behavior; consider stopping them.${NC}"
    echo ""
fi

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

# Follow logs in background so Ctrl+C hits our trap first.
# Default: hide noisy Postgres logs (healthchecks, checkpoints) to keep dev output usable.
# Set INCLUDE_DB_LOGS=1 to include Postgres.
LOG_SERVICES=(reverse-proxy zerg-backend zerg-frontend jarvis-web jarvis-server)
if [ "${INCLUDE_DB_LOGS:-0}" = "1" ]; then
    LOG_SERVICES+=(postgres)
fi

compose_cmd logs -f "${LOG_SERVICES[@]}" &
LOGS_PID=$!

# Wait for logs process (keeps script alive)
wait "$LOGS_PID"
