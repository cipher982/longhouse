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

.PHONY: help dev zerg jarvis stop logs reset test test-jarvis test-zerg generate-sdk seed-agents validate tool-check validate-ws regen-ws validate-makefile

# ---------------------------------------------------------------------------
# Help – `make` or `make help` (auto-generated from ## comments)
# ---------------------------------------------------------------------------
help: ## Show this help message
	@echo "\n🌐 Swarm Platform (Jarvis + Zerg)"
	@echo "=================================="
	@echo ""
	@grep -B0 '## ' Makefile | grep -E '^[a-zA-Z_-]+:' | sed 's/:.*## /: /' | column -t -s ':' | awk '{printf "  %-24s %s\n", $$1":", substr($$0, index($$0,$$2))}' | sort
	@echo ""

# ---------------------------------------------------------------------------
# Core Development Commands
# ---------------------------------------------------------------------------
dev: ## ⭐ Start full platform (Docker + Nginx, isolated ports)
	@echo "🚀 Starting full platform (Docker)..."
	@./scripts/dev-docker.sh

zerg: ## Start Zerg only (Postgres + Backend + Frontend)
	@echo "🚀 Starting Zerg platform..."
	docker compose -f docker/docker-compose.dev.yml up -d --build
	@sleep 3
	@docker compose -f docker/docker-compose.dev.yml ps
	@echo ""
	@echo "✅ Backend:  http://localhost:$${ZERG_BACKEND_PORT:-47300}"
	@echo "✅ Frontend: http://localhost:$${ZERG_FRONTEND_PORT:-47200}"

jarvis: ## Start Jarvis only (native Node processes)
	@echo "🤖 Starting Jarvis..."
	cd apps/jarvis && $(MAKE) start

stop: ## Stop all services (dev, zerg, jarvis)
	@echo "🛑 Stopping all services..."
	@docker compose -f docker/docker-compose.unified.yml down 2>/dev/null || true
	@docker compose -f docker/docker-compose.dev.yml down 2>/dev/null || true
	@cd apps/jarvis && $(MAKE) stop 2>/dev/null || true
	@echo "✅ All services stopped"

logs: ## View logs from running services
	@echo "📋 Checking for running services..."
	@if docker compose -f docker/docker-compose.unified.yml ps -q 2>/dev/null | grep -q .; then \
		echo "Following logs from unified dev environment..."; \
		docker compose -f docker/docker-compose.unified.yml logs -f; \
	elif docker compose -f docker/docker-compose.dev.yml ps -q 2>/dev/null | grep -q .; then \
		echo "Following logs from Zerg..."; \
		docker compose -f docker/docker-compose.dev.yml logs -f; \
	else \
		echo "❌ No services running. Start with 'make dev' or 'make zerg'"; \
		exit 1; \
	fi

reset: ## Reset database (destroys all data)
	@echo "⚠️  Resetting database..."
	@docker compose -f docker/docker-compose.dev.yml down -v
	@docker compose -f docker/docker-compose.dev.yml up -d
	@echo "✅ Database reset. Run 'make seed-agents' to populate."

# ---------------------------------------------------------------------------
# Testing targets
# ---------------------------------------------------------------------------

test: ## Run ALL tests (Jarvis + Zerg + integration)
	@echo "🧪 Running ALL tests (Jarvis + Zerg)..."
	$(MAKE) test-jarvis
	$(MAKE) test-zerg
	@echo "✅ All tests complete"

test-jarvis: ## Run Jarvis tests only
	@echo "🧪 Running Jarvis tests..."
	cd apps/jarvis/apps/web && bun vitest run --reporter=basic --silent

test-zerg: ## Run Zerg tests (backend + frontend + e2e)
	@echo "🧪 Running Zerg tests..."
	cd apps/zerg/backend && ./run_backend_tests.sh
	cd apps/zerg/frontend-web && bun run test
	cd apps/zerg/e2e && bunx playwright test

# ---------------------------------------------------------------------------
# SDK & Integration
# ---------------------------------------------------------------------------
generate-sdk: ## Generate OpenAPI/AsyncAPI clients and tool manifest
	@echo "🔄 Generating SDK..."
	@cd apps/zerg/backend && uv run python -m zerg.main --openapi-json > ../../../packages/contracts/openapi.json
	@cd packages/contracts && bun run generate
	@uv run python scripts/generate-tool-manifest.py
	@echo "✅ SDK generation complete"

seed-agents: ## Seed baseline Zerg agents for Jarvis
	@echo "🌱 Seeding agents..."
	@BACKEND=$$(docker ps --format "{{.Names}}" | grep "backend" | head -1); \
	if [ -z "$$BACKEND" ]; then \
		echo "❌ Backend not running. Start with 'make dev' or 'make zerg'"; \
		exit 1; \
	fi
	@docker exec $$BACKEND uv run python scripts/seed_jarvis_agents.py
	@echo "✅ Agents seeded"

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
validate: ## Run all validation checks
	@printf '\n🔍 Running all validation checks...\n\n'
	@printf '1️⃣  Validating WebSocket code...\n'
	@$(MAKE) validate-ws
	@printf '\n2️⃣  Validating Makefile structure...\n'
	@$(MAKE) validate-makefile
	@printf '\n3️⃣  Validating tool contracts...\n'
	@$(MAKE) tool-check
	@printf '\n✅ All validations passed\n'

tool-check: ## Validate tool contracts (for CI)
	@uv run python scripts/generate-tool-manifest.py --validate

validate-ws: ## Check WebSocket code is in sync (for CI)
	@bash scripts/regen-ws-code.sh >/dev/null 2>&1
	@if ! git diff --quiet; then \
		echo "❌ WebSocket code out of sync"; \
		echo "   Run 'make regen-ws' and commit changes"; \
		git diff; \
		exit 1; \
	fi
	@echo "✅ WebSocket code in sync"

regen-ws: ## Regenerate WebSocket contract code
	@echo "🔄 Regenerating WebSocket code..."
	@bash scripts/regen-ws-code.sh
	@echo "✅ WebSocket code regenerated"

# ---------------------------------------------------------------------------
# Makefile Validation
# ---------------------------------------------------------------------------
validate-makefile: ## Verify .PHONY targets match documented targets
	@failed=0; \
	\
	for t in $$(grep -E '^\.PHONY:' Makefile \
	          | sed -E 's/^\.PHONY:[[:space:]]*//; s/\\//g' \
	          | tr ' ' '\n' \
	          | sed '/^$$/d'); do \
	    case $$t in \
	        help|validate-makefile) continue ;; \
	    esac; \
	    if ! grep -Eq "^$$t:.*##" Makefile; then \
	        echo "❌ Missing help comment (##) for .PHONY target: $$t"; \
	        failed=1; \
	    fi; \
	done; \
	\
	for t in $$(grep -E '^[a-zA-Z0-9_-]+:.*##' Makefile \
	          | sed -E 's/:.*##.*$$//'); do \
	    if ! grep -Eq "^\.PHONY:.*\\b$$t\\b" Makefile; then \
	        echo "❌ Target has help but is not in .PHONY: $$t"; \
	        failed=1; \
	    fi; \
	done; \
	\
	if [ $$failed -eq 0 ]; then \
	    echo "✅ Makefile validation passed"; \
	fi; \
	exit $$failed
