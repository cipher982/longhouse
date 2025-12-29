# Swarm Platform (Jarvis + Zerg Monorepo)

# ---------------------------------------------------------------------------
# Load environment variables from .env
# ---------------------------------------------------------------------------
-include .env
export $(shell sed 's/=.*//' .env 2>/dev/null || true)

# Compose helpers (keep flags consistent across targets)
COMPOSE_DEV := docker compose --project-name zerg --env-file .env -f docker/docker-compose.dev.yml

.PHONY: help dev dev-bg stop logs logs-app logs-db doctor dev-clean dev-reset-db reset test test-unit test-e2e test-all test-chat-e2e test-e2e-single test-e2e-ui test-e2e-grep test-perf test-zerg-unit test-zerg-e2e generate-sdk seed-agents seed-credentials validate validate-ws regen-ws validate-sse regen-sse validate-makefile env-check env-check-prod smoke-prod perf-landing perf-gpu perf-gpu-dashboard

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
	for var in POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB APP_PUBLIC_URL; do \
		if [ -z "$$$(printenv $$var)" ]; then \
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
		   JWT_SECRET INTERNAL_API_SECRET FERNET_SECRET TRIGGER_SIGNING_SECRET \
		   GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET \
		   OPENAI_API_KEY ALLOWED_CORS_ORIGINS; do \
		if [ -z "$$$(printenv $$var)" ]; then \
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
dev: env-check ## ⭐ Start development environment (Docker + Nginx)
	@echo "🚀 Starting development environment (Docker)..."
	@./scripts/dev-docker.sh

dev-bg: env-check ## Start development environment in background
	@echo "🚀 Starting development environment (background)..."
	$(COMPOSE_DEV) --profile dev up -d --build --wait
	$(COMPOSE_DEV) --profile dev ps
	@echo "✅ Services started in background. Use 'make logs' to tail."

stop: ## Stop all Docker services
	@./scripts/stop-docker.sh

dev-clean: ## Stop/remove dev containers (keeps DB volume)
	@echo "🧹 Cleaning dev containers (keeping volumes)..."
	@$(COMPOSE_DEV) --profile dev down --remove-orphans 2>/dev/null || true
	@echo "✅ Cleaned dev containers (volumes preserved)"

dev-reset-db: ## Destroy dev DB volume (data loss)
	@echo "⚠️  Resetting dev database (THIS DELETES LOCAL DB DATA)..."
	@$(COMPOSE_DEV) --profile dev down -v --remove-orphans 2>/dev/null || true
	@echo "✅ DB reset. Start with 'make dev' and then run 'make seed-agents' if needed."

logs: ## View logs from running services
	@if $(COMPOSE_DEV) ps -q 2>/dev/null | grep -q .; then \
		$(COMPOSE_DEV) logs -f; \
	else \
		echo "❌ No services running. Start with 'make dev'"; \
		exit 1; \
	fi

logs-app: ## View logs for app services (excludes Postgres)
	@if $(COMPOSE_DEV) ps -q 2>/dev/null | grep -q .; then \
		$(COMPOSE_DEV) logs -f reverse-proxy backend frontend; \
	else \
		echo "❌ No services running. Start with 'make dev'"; \
		exit 1; \
	fi

logs-db: ## View logs for Postgres only
	@if $(COMPOSE_DEV) ps -q 2>/dev/null | grep -q .; then \
		$(COMPOSE_DEV) logs -f postgres; \
	else \
		echo "❌ No services running. Start with 'make dev'"; \
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
		if [ -z "$$$(printenv $$var)" ]; then \
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
	@$(COMPOSE_DEV) --profile dev up -d
	@echo "✅ Database reset. Run 'make seed-agents' to populate."

# ---------------------------------------------------------------------------
# Testing targets
# ---------------------------------------------------------------------------

# Playwright E2E should run against an isolated frontend/backend pair by default,
# not whatever is currently running in Docker on the dev ports.
# Override per-command if you intentionally want to target your dev stack:
#   make test-e2e E2E_BACKEND_PORT=47300 E2E_FRONTEND_PORT=47200
E2E_BACKEND_PORT ?= 8001
E2E_FRONTEND_PORT ?= 8002

test: ## Run fast unit tests (backend + frontend)
	@echo "🧪 Running unit tests (no Playwright E2E)..."
	$(MAKE) test-unit

test-unit: ## Alias: unit tests only
	$(MAKE) test-zerg-unit

test-e2e: ## Alias: Playwright E2E only
	$(MAKE) test-zerg-e2e

test-all: ## Run unit + Playwright E2E
	@echo "🧪 Running full suite (unit + Playwright E2E)..."
	$(MAKE) test-unit
	$(MAKE) test-e2e

test-chat-e2e: ## Run Jarvis chat E2E tests (inside unified SPA)
	@echo "🧪 Running chat E2E tests (unified SPA)..."
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test tests/unified-frontend.spec.ts

test-e2e-single: ## Run a single E2E test (usage: make test-e2e-single TEST=tests/unified-frontend.spec.ts)
	@test -n "$(TEST)" || (echo "❌ Usage: make test-e2e-single TEST=<spec-or-args>" && exit 1)
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test $(TEST)

test-e2e-ui: ## Run Playwright E2E tests with interactive UI
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test --ui

test-e2e-grep: ## Run E2E tests by name (usage: make test-e2e-grep GREP="test name")
	@test -n "$(GREP)" || (echo "❌ Usage: make test-e2e-grep GREP='test name'" && exit 1)
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test --grep "$(GREP)"

test-perf: ## Run performance evaluation tests (chat latency profiling)
	@echo "🧪 Running performance evaluation tests..."
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test tests/chat_performance_eval.spec.ts
	@echo "✅ Performance tests complete. Metrics exported to apps/zerg/e2e/metrics/"

test-zerg-unit: ## Run Zerg unit tests (backend + frontend)
	@echo "🧪 Running Zerg unit tests..."
	cd apps/zerg/backend && ./run_backend_tests.sh
	cd apps/zerg/frontend-web && bun run test

test-zerg-e2e: ## Run Zerg E2E tests (Playwright)
	@echo "🧪 Running Zerg E2E tests..."
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test

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
		echo "❌ Backend not running. Start with 'make dev'"; \
		exit 1; \
	fi
	@docker exec $$BACKEND uv run python scripts/seed_jarvis_agents.py
	@echo "✅ Agents seeded"

seed-credentials: ## Seed personal tool credentials (Traccar, WHOOP, Obsidian)
	@echo "🔑 Seeding personal credentials..."
	@BACKEND=$$(docker ps --format "{{.Names}}" | grep "backend" | head -1); \
	if [ -z "$$BACKEND" ]; then \
		echo "❌ Backend not running. Start with 'make dev'"; \
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
	@printf '\n2️⃣  Validating SSE code...\n'
	@$(MAKE) validate-sse
	@printf '\n3️⃣  Validating Makefile structure...\n'
	@$(MAKE) validate-makefile
	@printf '\n✅ All validations passed\n'

validate-ws: ## Check WebSocket code is in sync (for CI)
	@bash scripts/regen-ws-code.sh >/dev/null 2>&1
	@# Only check for drift in generated files to avoid false positives from unrelated changes
	@if ! git diff --quiet \
		apps/zerg/backend/zerg/generated/ws_messages.py \
		apps/zerg/frontend-web/src/generated/ws-messages.ts \
		schemas/ws-protocol.schema.json \
		schemas/ws-protocol-v1.json; then \
		echo "❌ WebSocket code out of sync"; \
		echo "   Run 'make regen-ws' and commit changes"; \
		git diff \
			apps/zerg/backend/zerg/generated/ws_messages.py \
			apps/zerg/frontend-web/src/generated/ws-messages.ts \
			schemas/ws-protocol.schema.json \
			schemas/ws-protocol-v1.json; \
		exit 1; \
	fi
	@echo "✅ WebSocket code in sync"

regen-ws: ## Regenerate WebSocket contract code
	@echo "🔄 Regenerating WebSocket code..."
	@bash scripts/regen-ws-code.sh
	@echo "✅ WebSocket code regenerated"

validate-sse: ## Check SSE code is in sync (for CI)
	@bash scripts/regen-sse-code.sh >/dev/null 2>&1
	@# Only check for drift in generated files to avoid false positives from unrelated changes
	@if ! git diff --quiet \
		apps/zerg/backend/zerg/generated/sse_events.py \
		apps/zerg/frontend-web/src/generated/sse-events.ts \
		schemas/sse-events.asyncapi.yml; then \
		echo "❌ SSE code out of sync"; \
		echo "   Run 'make regen-sse' and commit changes"; \
		git diff \
			apps/zerg/backend/zerg/generated/sse_events.py \
			apps/zerg/frontend-web/src/generated/sse-events.ts \
			schemas/sse-events.asyncapi.yml; \
		exit 1; \
	fi
	@echo "✅ SSE code in sync"

regen-sse: ## Regenerate SSE event contract code
	@echo "🔄 Regenerating SSE code..."
	@bash scripts/regen-sse-code.sh
	@echo "✅ SSE code regenerated"

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

# ---------------------------------------------------------------------------
# Performance Profiling
# ---------------------------------------------------------------------------
perf-landing: ## Profile landing page rendering events (Chrome trace analysis)
	@echo "🔬 Profiling landing page rendering..."
	@echo "   Ensure 'make dev' is running first!"
	@echo ""
	cd apps/zerg/e2e && bun run scripts/profile-landing.ts \
		--url=http://localhost:30080 \
		--duration=10 \
		--output=./perf-results \
		$(ARGS)
	@echo ""
	@echo "📊 Results in apps/zerg/e2e/perf-results/"
	@echo "   - report.md: Human-readable summary"
	@echo "   - trace-*.json: Open in Chrome DevTools or https://ui.perfetto.dev"

perf-gpu: ## Measure actual GPU utilization % for landing page effects (macOS)
	@echo "🔬 Measuring GPU utilization..."
	@echo "   Ensure 'make dev' is running first!"
	@echo "   This measures actual GPU % from macOS (same as Activity Monitor)"
	@echo ""
	cd apps/zerg/e2e && bun run scripts/gpu-profiler.ts \
		--url=http://localhost:30080 \
		--duration=10 \
		--output=./perf-results \
		$(ARGS)
	@echo ""
	@echo "📊 Results in apps/zerg/e2e/perf-results/"
	@echo "   - gpu-report.md: Human-readable summary"
	@echo "   - gpu-summary.json: Stats per variant"
	@echo "   - gpu-samples.json: Raw sample data"

perf-gpu-dashboard: ## Measure actual GPU utilization % for dashboard ui-effects on/off (macOS)
	@echo "🔬 Measuring GPU utilization (dashboard)..."
	@echo "   Ensure 'make dev' is running first!"
	@echo "   This measures actual GPU % from macOS (same as Activity Monitor)"
	@echo ""
	cd apps/zerg/e2e && bun run scripts/gpu-profiler-dashboard.ts \
		--url=http://localhost:30080 \
		--duration=10 \
		--output=./perf-results \
		$(ARGS)
	@echo ""
	@echo "📊 Results in apps/zerg/e2e/perf-results/"
	@echo "   - gpu-dashboard-report.md: Human-readable summary"
	@echo "   - gpu-dashboard-summary.json: Stats per variant"
	@echo "   - gpu-dashboard-samples.json: Raw sample data"
