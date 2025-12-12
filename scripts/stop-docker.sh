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

# Important: this repo uses Compose profiles. Without `--profile`, `down`/`stop`
# only act on the default (unprofiled) services (e.g. `postgres`) and will NOT
# stop the full stack.
compose_down() {
  if [ "$QUIET" = "1" ]; then
    compose_cmd --profile "$1" down --remove-orphans >/dev/null 2>&1 || true
  else
    compose_cmd --profile "$1" down --remove-orphans 2>/dev/null || true
  fi
}

# Tear down both common dev profiles.
compose_down full
compose_down zerg

if [ "$QUIET" = "0" ]; then
  echo "✅ All services stopped"
fi
