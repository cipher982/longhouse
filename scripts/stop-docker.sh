#!/bin/bash
# Stop all zerg dev docker-compose services (shared by `make stop` and Ctrl+C cleanup)
set -euo pipefail

QUIET=0
if [ "${1:-}" = "--quiet" ]; then
  QUIET=1
fi

COMPOSE_PROJECT_NAME="zerg"
COMPOSE_FILE="docker/docker-compose.dev.yml"
ENV_FILE=".env"

if [ ! -f "$ENV_FILE" ]; then
  # Stopping should still be possible even if `.env` is missing,
  # but we keep this loud so you notice the repo is half-configured.
  echo "❌ Missing $ENV_FILE file"
  exit 1
fi

compose_cmd() {
  docker compose --project-name "$COMPOSE_PROJECT_NAME" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

if [ "$QUIET" = "0" ]; then
  echo "🛑 Stopping all services..."
fi

# `down` stops/removes all containers in the project regardless of profile.
if [ "$QUIET" = "1" ]; then
  compose_cmd down --remove-orphans >/dev/null 2>&1 || true
else
  compose_cmd down --remove-orphans 2>/dev/null || true
fi

if [ "$QUIET" = "0" ]; then
  echo "✅ All services stopped"
fi
