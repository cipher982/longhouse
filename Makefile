# Swarm Platform (Jarvis + Zerg Monorepo)

# ---------------------------------------------------------------------------
# Load environment variables from .env (ports are now configured there)
# ---------------------------------------------------------------------------
-include .env
export $(shell sed 's/=.*//' .env 2>/dev/null || true)

# Fallback defaults if .env is missing values
ZERG_BACKEND_PORT ?= $(BACKEND_PORT)
ZERG_FRONTEND_PORT ?= $(FRONTEND_PORT)
ZERG_BACKEND_PORT ?= 47300
ZERG_FRONTEND_PORT ?= 47200
JARVIS_SERVER_PORT ?= 8787
JARVIS_WEB_PORT ?= 8080

.PHONY: help jarvis-dev test generate-sdk generate-tools seed-jarvis-agents test-jarvis test-zerg zerg-up zerg-down zerg-logs zerg-reset regen-ws-code ws-code-diff-check

# ---------------------------------------------------------------------------
# Help – `make` or `make help`
# ---------------------------------------------------------------------------
help:
	@echo "\n🌐 Swarm Platform (Jarvis + Zerg)"
	@echo "=================================="
	@echo ""
	@echo "Quick Start:"
	@echo "  make zerg-up       Start EVERYTHING (Postgres + Backend + Frontend via docker-compose)"
	@echo "  make zerg-down     Stop EVERYTHING"
	@echo "  make zerg-logs     View all logs"
	@echo "  make zerg-reset    Reset database (destroys all data)"
	@echo ""
	@echo "Development:"
	@echo "  make jarvis-dev            Start Jarvis PWA separately (ports $(JARVIS_SERVER_PORT), $(JARVIS_WEB_PORT))"
	@echo "  make generate-sdk          Generate OpenAPI/AsyncAPI clients and tool manifest"
	@echo "  make generate-tools        Generate tool manifest only"
	@echo "  make seed-jarvis-agents    Seed baseline Zerg agents for Jarvis integration"
	@echo ""
	@echo "Testing:"
	@echo "  make test                  Run ALL tests (Jarvis + Zerg + integration)"
	@echo "  make test-jarvis           Run Jarvis tests only"
	@echo "  make test-zerg             Run Zerg tests (backend + frontend + e2e)"
	@echo ""

# ---------------------------------------------------------------------------
# Development workflow – Jarvis
# ---------------------------------------------------------------------------
jarvis-dev:
	@echo "🤖 Starting Jarvis (PWA + Node server)..."
	cd apps/jarvis && $(MAKE) start

# ---------------------------------------------------------------------------
# Testing targets
# ---------------------------------------------------------------------------

test:
	@echo "🧪 Running ALL tests (Jarvis + Zerg)..."
	$(MAKE) test-jarvis
	$(MAKE) test-zerg
	@echo "✅ All tests complete"

test-jarvis:
	@echo "🧪 Running Jarvis tests..."
	cd apps/jarvis && npm test

test-zerg:
	@echo "🧪 Running Zerg tests..."
	cd apps/zerg/backend && ./run_backend_tests.sh
	cd apps/zerg/frontend-web && npm test
	cd apps/zerg/e2e && npx playwright test

# ---------------------------------------------------------------------------
# SDK Generation
# ---------------------------------------------------------------------------
generate-sdk:
	@echo "🔄 Generating OpenAPI/AsyncAPI clients and tool manifest..."
	@echo "📡 Generating from Zerg backend..."
	cd apps/zerg/backend && uv run python -m zerg.main --openapi-json > ../../../packages/contracts/openapi.json
	@echo "📦 Generating TypeScript clients..."
	cd packages/contracts && npm run generate
	@echo "🔧 Generating tool manifest..."
	python3 scripts/generate-tool-manifest.py
	@echo "✅ SDK generation complete"

generate-tools:
	@echo "🔧 Generating tool manifest only..."
	python3 scripts/generate-tool-manifest.py

# ---------------------------------------------------------------------------
# Jarvis Integration
# ---------------------------------------------------------------------------
seed-jarvis-agents:
	@echo "🌱 Seeding baseline Zerg agents for Jarvis..."
	@echo ""
	@# Check if the backend container is running
	@BACKEND_CONTAINER=$$(docker ps --format "{{.Names}}" | grep "zerg-backend" | head -1); \
	if [ -z "$$BACKEND_CONTAINER" ]; then \
		echo "❌ Backend container is not running."; \
		echo ""; \
		echo "Please start the platform first:"; \
		echo "  make zerg-up"; \
		echo ""; \
		exit 1; \
	fi
	@# Check if postgres is healthy
	@POSTGRES_CONTAINER=$$(docker ps --format "{{.Names}}" | grep "zerg-postgres" | head -1); \
	if [ -z "$$POSTGRES_CONTAINER" ]; then \
		echo "❌ Database container is not running."; \
		echo "Please start the platform first:"; \
		echo "  make zerg-up"; \
		exit 1; \
	fi
	@echo "✅ Containers are running"
	@echo "   Backend: zerg-backend-1"
	@echo "   Database: zerg-postgres-1"
	@echo ""
	@# Run the seeding script inside the backend container
	docker exec zerg-backend-1 uv run python scripts/seed_jarvis_agents.py
	@echo ""
	@echo "✅ Agents seeded successfully"
# ---------------------------------------------------------------------------
# Docker Compose - Everything together
# ---------------------------------------------------------------------------
zerg-up:
	@echo "🚀 Starting Zerg platform (Dev environment with hot-reload)..."
	@echo "   Using: docker-compose.dev.yml (development with volume mounts)"
	docker compose -f docker-compose.dev.yml up -d --build
	@sleep 3
	@docker compose -f docker-compose.dev.yml ps
	@echo ""
	@echo "✅ Platform started!"
	@echo "   Backend: http://localhost:$(ZERG_BACKEND_PORT)"
	@echo "   Frontend: http://localhost:$(ZERG_FRONTEND_PORT)"

zerg-down:
	@echo "🛑 Stopping Zerg platform..."
	docker compose -f docker-compose.dev.yml down
	@echo "✅ All stopped"

zerg-logs:
	docker compose -f docker-compose.dev.yml logs -f

zerg-reset:
	@echo "⚠️  Resetting database (destroys all data)..."
	docker compose -f docker-compose.dev.yml down -v
	docker compose -f docker-compose.dev.yml up -d
	@echo "Run migrations and seed agents"

# ---------------------------------------------------------------------------
# WebSocket Code Generation
# ---------------------------------------------------------------------------
regen-ws-code:
	@echo "🔄 Regenerating WebSocket contract code..."
	@bash scripts/regen-ws-code.sh
	@echo "✅ WebSocket code regenerated"

ws-code-diff-check:
	@echo "🔍 Checking WebSocket code is in sync with asyncapi/chat.yml..."
	@bash scripts/regen-ws-code.sh
	@if git diff --quiet; then \
		echo "✅ WebSocket code is in sync"; \
	else \
		echo "❌ WebSocket code is out of sync with asyncapi/chat.yml"; \
		echo "   Please run: make regen-ws-code"; \
		echo "   Then commit the changes"; \
		git diff; \
		exit 1; \
	fi

