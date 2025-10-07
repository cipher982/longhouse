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

.PHONY: help start stop jarvis-dev zerg-dev swarm-dev test generate-sdk generate-tools seed-jarvis-agents validate-contracts validate-deploy test-jarvis test-zerg

# ---------------------------------------------------------------------------
# Help – `make` or `make help`
# ---------------------------------------------------------------------------
help:
	@echo "\n🌐 Swarm Platform (Jarvis + Zerg)"
	@echo "=================================="
	@echo ""
	@echo "Development:"
	@echo "  make jarvis-dev    Start Jarvis PWA + node server (ports $(JARVIS_SERVER_PORT), $(JARVIS_WEB_PORT))"
	@echo "  make zerg-dev      Start Zerg backend + frontend (ports $(ZERG_BACKEND_PORT), $(ZERG_FRONTEND_PORT))"
	@echo "  make swarm-dev     Start BOTH Jarvis and Zerg concurrently"
	@echo "  make stop          Stop all development servers"
	@echo "  make generate-sdk  Generate OpenAPI/AsyncAPI clients and tool manifest"
	@echo "  make generate-tools Generate tool manifest only"
	@echo "  make seed-jarvis-agents  Seed baseline Zerg agents for Jarvis integration"
	@echo ""
	@echo "Testing:"
	@echo "  make test          Run ALL tests (Jarvis + Zerg + integration)"
	@echo "  make test-jarvis   Run Jarvis tests only"
	@echo "  make test-zerg     Run Zerg tests (backend + frontend + e2e)"
	@echo "  make validate-contracts  Run contract validation checks"
	@echo ""
	@echo "Deployment:"
	@echo "  make validate-deploy    Validate environment for deployment"
	@echo ""

# ---------------------------------------------------------------------------
# Development workflow – Individual apps
# ---------------------------------------------------------------------------
jarvis-dev:
	@echo "🤖 Starting Jarvis (PWA + Node server)..."
	cd apps/jarvis && $(MAKE) start

zerg-dev:
	@echo "🐝 Starting Zerg (FastAPI backend + frontend)..."
	@echo "🚀 Starting development servers on ports $(ZERG_BACKEND_PORT) and $(ZERG_FRONTEND_PORT)..."
	cd apps/zerg && ./scripts/fast-contract-check.sh
	$(MAKE) -j2 _zerg_backend _zerg_frontend

swarm-dev:
	@echo "🌐 Starting FULL SWARM (Jarvis + Zerg)..."
	@echo "   Jarvis: http://localhost:$(JARVIS_WEB_PORT)"
	@echo "   Zerg Backend: http://localhost:$(ZERG_BACKEND_PORT)"
	@echo "   Zerg Frontend: http://localhost:$(ZERG_FRONTEND_PORT)"
	$(MAKE) -j2 jarvis-dev zerg-dev

_zerg_backend:
	cd apps/zerg/backend && uv run python -m uvicorn zerg.main:app --reload --port $(ZERG_BACKEND_PORT)

_zerg_frontend:
	cd apps/zerg/frontend && ./build-debug.sh

stop:
	@echo "🛑 Stopping all Swarm services..."
	- lsof -ti:$(ZERG_BACKEND_PORT) | xargs kill 2>/dev/null || true
	- lsof -ti:$(ZERG_FRONTEND_PORT) | xargs kill 2>/dev/null || true
	- lsof -ti:$(JARVIS_SERVER_PORT) | xargs kill 2>/dev/null || true
	- lsof -ti:$(JARVIS_WEB_PORT) | xargs kill 2>/dev/null || true
	@cd apps/jarvis && $(MAKE) stop 2>/dev/null || true
	@echo "✅ Stopped"

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
	cd apps/zerg/frontend && ./run_frontend_tests.sh
	cd apps/zerg/e2e && ./run_e2e_tests.sh --mode=basic

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
	cd apps/zerg/backend && uv run python scripts/seed_jarvis_agents.py
	@echo "✅ Agents seeded"

# ---------------------------------------------------------------------------
# Contract validation (catches API mismatches at build time)
# ---------------------------------------------------------------------------
validate-contracts:
	@echo "🔍 Running API contract validation..."
	cd apps/zerg && ./scripts/fast-contract-check.sh

# ---------------------------------------------------------------------------
# Deployment validation (checks environment and connectivity)
# ---------------------------------------------------------------------------
validate-deploy:
	@echo "🔍 Validating deployment configuration..."
	cd apps/zerg/backend && uv run python ../scripts/validate-deployment.py