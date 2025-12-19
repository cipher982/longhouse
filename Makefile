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
JARVIS_WEB_PORT ?= 8080

# Compose helpers (keep flags consistent across targets)
COMPOSE_DEV := docker compose --project-name zerg --env-file .env -f docker/docker-compose.dev.yml
COMPOSE_E2E := docker compose --project-name zerg-e2e -f apps/jarvis/docker-compose.test.yml

.PHONY: help dev zerg jarvis jarvis-stop stop logs logs-app logs-db doctor dev-clean dev-reset-db reset test test-jarvis test-jarvis-unit test-jarvis-watch test-jarvis-e2e test-jarvis-e2e-ui test-jarvis-text test-jarvis-history test-jarvis-grep test-e2e-up test-e2e-down test-e2e-single test-zerg generate-sdk seed-agents seed-credentials validate validate-ws regen-ws validate-makefile env-check env-check-prod smoke-prod

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
# Environment Validation
# ---------------------------------------------------------------------------
env-check: ## Validate required environment variables
	@missing=0; \
	warn=0; \
	echo "🔍 Checking environment variables..."; \
	\
	for var in POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB; do \
		if [ -z "$$(eval echo \$$$$var)" ]; then \
			echo "❌ Missing required: $$var"; \
			missing=1; \
		fi; \
	done; \
	\
	if [ -z "$$OPENAI_API_KEY" ]; then \
		echo "⚠️  Warning: OPENAI_API_KEY not set (LLM features disabled)"; \
		warn=1; \
	fi; \
	\
	if [ $$missing -eq 1 ]; then \
		echo ""; \
		echo "💡 Copy .env.example to .env and fill in required values"; \
		exit 1; \
	fi; \
	\
	if [ $$warn -eq 0 ]; then \
		echo "✅ All required environment variables set"; \
	else \
		echo "✅ Required variables set (warnings above are optional)"; \
	fi

env-check-prod: ## Validate production environment variables
	@missing=0; \
	echo "🔍 Checking production environment variables..."; \
	\
	for var in POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB \
	           JWT_SECRET FERNET_SECRET TRIGGER_SIGNING_SECRET \
	           GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET \
	           OPENAI_API_KEY ALLOWED_CORS_ORIGINS; do \
		if [ -z "$$(eval echo \$$$$var)" ]; then \
			echo "❌ Missing required for prod: $$var"; \
			missing=1; \
		fi; \
	done; \
	\
	if [ "$$AUTH_DISABLED" = "1" ]; then \
		echo "❌ AUTH_DISABLED must be 0 for production"; \
		missing=1; \
	fi; \
	\
	if [ $$missing -eq 1 ]; then \
		echo ""; \
		echo "💡 Set all required production variables before deploying"; \
		exit 1; \
	fi; \
	echo "✅ All production environment variables set"

# ---------------------------------------------------------------------------
# Core Development Commands
# ---------------------------------------------------------------------------
dev: env-check ## ⭐ Start full platform (Docker + Nginx, isolated ports)
	@echo "🚀 Starting full platform (Docker)..."
	@./scripts/dev-docker.sh

zerg: env-check ## Start Zerg only (Postgres + Backend + Frontend)
	@echo "🚀 Starting Zerg platform..."
	$(COMPOSE_DEV) --profile zerg up -d --build
	@sleep 3
	@$(COMPOSE_DEV) --profile zerg ps
	@echo ""
	@echo "✅ Backend:  http://localhost:$${BACKEND_PORT:-47300}"
	@echo "✅ Frontend: http://localhost:$${FRONTEND_PORT:-47200}"

jarvis: ## Start Jarvis standalone (native, no Docker)
	@echo "🤖 Starting Jarvis (native mode)..."
	@echo "   For Docker mode, use 'make dev'"
	cd apps/jarvis && $(MAKE) start

jarvis-stop: ## Stop native Jarvis processes (for 'make jarvis')
	@cd apps/jarvis && $(MAKE) stop

stop: ## Stop all Docker services
	@./scripts/stop-docker.sh

dev-clean: ## Stop/remove zerg dev containers (keeps DB volume)
	@echo "🧹 Cleaning zerg dev containers (keeping volumes)..."
	@$(COMPOSE_DEV) --profile full down --remove-orphans 2>/dev/null || true
	@$(COMPOSE_DEV) --profile zerg down --remove-orphans 2>/dev/null || true
	@echo "✅ Cleaned zerg containers (volumes preserved)"

dev-reset-db: ## Destroy zerg dev DB volume (data loss)
	@echo "⚠️  Resetting zerg dev database (THIS DELETES LOCAL DB DATA)..."
	@$(COMPOSE_DEV) --profile full down -v --remove-orphans 2>/dev/null || true
	@$(COMPOSE_DEV) --profile zerg down -v --remove-orphans 2>/dev/null || true
	@echo "✅ Zerg DB reset. Start with 'make dev' and then run 'make seed-agents' if needed."

logs: ## View logs from running services
	@if $(COMPOSE_DEV) ps -q 2>/dev/null | grep -q .; then \
		$(COMPOSE_DEV) logs -f; \
	else \
		echo "❌ No services running. Start with 'make dev' or 'make zerg'"; \
		exit 1; \
	fi

logs-app: ## View logs for app services (excludes Postgres)
	@if $(COMPOSE_DEV) ps -q 2>/dev/null | grep -q .; then \
		$(COMPOSE_DEV) logs -f reverse-proxy zerg-backend zerg-backend-exposed zerg-frontend zerg-frontend-exposed jarvis-web; \
	else \
		echo "❌ No services running. Start with 'make dev' or 'make zerg'"; \
		exit 1; \
	fi

logs-db: ## View logs for Postgres only
	@if $(COMPOSE_DEV) ps -q 2>/dev/null | grep -q .; then \
		$(COMPOSE_DEV) logs -f postgres; \
	else \
		echo "❌ No services running. Start with 'make dev' or 'make zerg'"; \
		exit 1; \
	fi

doctor: ## Print quick diagnostics for dev stack
	@echo "🔎 Swarm dev diagnostics"
	@echo "  - Repo:   $$(pwd)"
	@echo "  - Branch: $$(git rev-parse --abbrev-ref HEAD)"
	@echo ""
	@echo "📄 .env (presence + required vars)"
	@test -f .env && echo "  ✅ .env exists" || (echo "  ❌ missing .env" && exit 1)
	@missing=0; \
	for var in POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB; do \
		if [ -z "$$(eval echo \$$$$var)" ]; then \
			echo "  ❌ $$var is empty"; \
			missing=1; \
		else \
			echo "  ✅ $$var is set"; \
		fi; \
	done; \
	if [ $$missing -eq 1 ]; then exit 1; fi
	@echo ""
	@echo "🐳 Docker"
	@docker version >/dev/null 2>&1 && echo "  ✅ docker is reachable" || (echo "  ❌ docker not reachable" && exit 1)
	@echo ""
	@echo "🧩 Compose config (resolved env interpolation)"
	@$(COMPOSE_DEV) config >/dev/null && echo "  ✅ compose config renders" || (echo "  ❌ compose config failed" && exit 1)
	@echo ""
	@echo "📦 Running services (zerg project)"
	@$(COMPOSE_DEV) ps

reset: ## Reset database (destroys all data)
	@echo "⚠️  Resetting database..."
	@$(COMPOSE_DEV) down -v 2>/dev/null || true
	@$(COMPOSE_DEV) --profile zerg up -d
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

# Granular Jarvis test targets (for faster iteration)
test-jarvis-unit: ## Run Jarvis unit tests (no Docker)
	@echo "🧪 Running Jarvis unit tests (fast)..."
	cd apps/jarvis/apps/web && npm test -- --run

test-jarvis-watch: ## Run Jarvis unit tests in watch mode (TDD)
	@echo "🧪 Running Jarvis unit tests in watch mode..."
	cd apps/jarvis/apps/web && npm test -- --watch

test-jarvis-e2e: ## Run Jarvis E2E tests (isolated Docker environment)
	@echo "🧪 Running Jarvis E2E tests (isolated)..."
	@$(MAKE) test-e2e-up
	$(COMPOSE_E2E) run --rm playwright npx playwright test
	@$(MAKE) test-e2e-down

test-e2e-up: ## Start isolated E2E test environment
	@echo "🚀 Starting isolated E2E environment..."
	$(COMPOSE_E2E) up -d --build
	@echo "⏳ Waiting for services to be healthy..."
	@for i in {1..30}; do \
		if [ "$$($(COMPOSE_E2E) ps | grep "(healthy)" | wc -l)" -ge 4 ]; then \
			echo "✅ E2E environment is ready"; \
			exit 0; \
		fi; \
		sleep 2; \
	done; \
	echo "❌ Timeout waiting for services to be healthy"; \
	$(COMPOSE_E2E) ps; \
	exit 1

test-e2e-down: ## Stop and clean up isolated E2E environment
	@echo "🧹 Cleaning up E2E environment..."
	$(COMPOSE_E2E) down -v --remove-orphans

test-e2e-reset: ## Reset E2E database and environment
	@$(MAKE) test-e2e-down
	@$(MAKE) test-e2e-up

test-e2e-single: ## Run a single E2E test (usage: make test-e2e-single TEST=supervisor-progress)
	@test -n "$(TEST)" || (echo "❌ Usage: make test-e2e-single TEST=test-name" && exit 1)
	@echo "🧪 Running single E2E test: $(TEST)..."
	@if ! $(COMPOSE_E2E) ps reverse-proxy | grep -q "Up"; then $(MAKE) test-e2e-up; fi
	$(COMPOSE_E2E) run --rm playwright npx playwright test $(TEST)

test-e2e-logs: ## View logs for isolated E2E environment
	$(COMPOSE_E2E) logs -f

test-jarvis-e2e-ui: ## Run Jarvis E2E tests with interactive UI
	@echo "🧪 Running Jarvis E2E tests (UI mode)..."
	cd apps/jarvis && npx playwright test --ui

test-jarvis-text: ## Run text message E2E tests only
	@echo "🧪 Running text message tests..."
	@if ! $(COMPOSE_E2E) ps reverse-proxy | grep -q "Up"; then $(MAKE) test-e2e-up; fi
	$(COMPOSE_E2E) run --rm playwright npx playwright test text-message-happy-path

test-jarvis-history: ## Run history hydration E2E tests only
	@echo "🧪 Running history hydration tests..."
	@if ! $(COMPOSE_E2E) ps reverse-proxy | grep -q "Up"; then $(MAKE) test-e2e-up; fi
	$(COMPOSE_E2E) run --rm playwright npx playwright test history-hydration

test-jarvis-grep: ## Run specific test by name (usage: make test-jarvis-grep GREP="test name")
	@test -n "$(GREP)" || (echo "❌ Usage: make test-jarvis-grep GREP='test name'" && exit 1)
	@if ! $(COMPOSE_E2E) ps reverse-proxy | grep -q "Up"; then $(MAKE) test-e2e-up; fi
	$(COMPOSE_E2E) run --rm playwright npx playwright test --grep "$(GREP)"

test-zerg: ## Run Zerg tests (backend + frontend + e2e)
	@echo "🧪 Running Zerg tests..."
	cd apps/zerg/backend && ./run_backend_tests.sh
	cd apps/zerg/frontend-web && bun run test
	cd apps/zerg/e2e && bunx playwright test

# ---------------------------------------------------------------------------
# SDK & Integration
# ---------------------------------------------------------------------------
generate-sdk: ## Generate OpenAPI types from backend schema
	@echo "🔄 Generating SDK..."
	@cd apps/zerg/backend && uv run python -c "from zerg.main import app; app.openapi()" 2>/dev/null
	@cd apps/zerg/frontend-web && bun run generate:api
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

seed-credentials: ## Seed personal tool credentials (Traccar, WHOOP, Obsidian)
	@echo "🔑 Seeding personal credentials..."
	@BACKEND=$$(docker ps --format "{{.Names}}" | grep "backend" | head -1); \
	if [ -z "$$BACKEND" ]; then \
		echo "❌ Backend not running. Start with 'make dev' or 'make zerg'"; \
		exit 1; \
	fi
	@docker exec $$BACKEND uv run python scripts/seed_personal_credentials.py $(ARGS)
	@echo "✅ Credentials seeded"

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
validate: ## Run all validation checks
	@printf '\n🔍 Running all validation checks...\n\n'
	@printf '1️⃣  Validating WebSocket code...\n'
	@$(MAKE) validate-ws
	@printf '\n2️⃣  Validating Makefile structure...\n'
	@$(MAKE) validate-makefile
	@printf '\n✅ All validations passed\n'

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

# ---------------------------------------------------------------------------
# Production Smoke Tests
# ---------------------------------------------------------------------------
smoke-prod: ## Run production smoke tests (validates deployed instance)
	@./scripts/smoke-prod.sh
