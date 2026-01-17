# Swarm Platform (Jarvis + Zerg Monorepo)

# ---------------------------------------------------------------------------
# Load environment variables from .env
# ---------------------------------------------------------------------------
-include .env
export $(shell sed 's/=.*//' .env 2>/dev/null || true)

# Compose helpers (keep flags consistent across targets)
COMPOSE_DEV := docker compose --project-name zerg --env-file .env -f docker/docker-compose.dev.yml

.PHONY: help dev dev-bg stop logs logs-app logs-db doctor dev-clean dev-reset-db reset test test-unit test-e2e test-e2e-core test-all test-chat-e2e test-e2e-single test-e2e-ui test-e2e-verbose test-e2e-errors test-e2e-query test-e2e-grep test-perf test-zerg-unit test-zerg-e2e test-frontend-unit test-prompts eval eval-live eval-compare eval-critical eval-fast eval-all eval-tool-selection generate-sdk seed-agents seed-credentials seed-marketing marketing-capture marketing-single marketing-validate marketing-list validate validate-ws regen-ws validate-sse regen-sse validate-makefile lint-test-patterns env-check env-check-prod smoke-prod perf-landing perf-gpu perf-gpu-dashboard debug-thread debug-validate debug-inspect debug-batch debug-trace trace-coverage


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

test: ## Backend + frontend tests (no Playwright)
	@echo "🧪 Running tests (no Playwright E2E)..."
	$(MAKE) test-unit

test-unit: ## Alias for test-zerg-unit
	$(MAKE) test-zerg-unit

test-e2e: ## Alias for test-zerg-e2e
	$(MAKE) test-zerg-e2e

test-e2e-core: ## Run core E2E tests only (no retries, must pass 100%)
	@echo "🔴 Running CORE E2E tests (no retries, must pass 100%)..."
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test --project=core

test-all: ## Full suite including Playwright E2E
	@echo "🧪 Running full suite (unit + Playwright E2E)..."
	$(MAKE) test-unit
	$(MAKE) test-e2e

test-chat-e2e: ## Run Jarvis chat E2E tests (inside unified SPA)
	@echo "🧪 Running chat E2E tests (unified SPA)..."
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test --project=chromium tests/unified-frontend.spec.ts

test-e2e-single: ## Run a single E2E test (usage: make test-e2e-single TEST=tests/unified-frontend.spec.ts)
	@test -n "$(TEST)" || (echo "❌ Usage: make test-e2e-single TEST=<spec-or-args>" && exit 1)
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test $(TEST)

test-e2e-ui: ## Run Playwright E2E tests with interactive UI
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test --project=chromium --ui

test-e2e-verbose: ## Run E2E tests with full verbose output (for debugging)
	cd apps/zerg/e2e && VERBOSE=1 BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test --project=chromium

test-e2e-errors: ## Show detailed errors from last E2E run
	@if [ -f apps/zerg/e2e/test-results/errors.txt ]; then \
		cat apps/zerg/e2e/test-results/errors.txt; \
	else \
		echo "No errors.txt found. Run 'make test-e2e' first."; \
	fi

test-e2e-query: ## Query last E2E results (usage: make test-e2e-query Q='.failed[]')
	@if [ -f apps/zerg/e2e/test-results/summary.json ]; then \
		jq '$(Q)' apps/zerg/e2e/test-results/summary.json; \
	else \
		echo "No summary.json found. Run 'make test-e2e' first."; \
	fi

test-e2e-grep: ## Run E2E tests by name (usage: make test-e2e-grep GREP="test name")
	@test -n "$(GREP)" || (echo "❌ Usage: make test-e2e-grep GREP='test name'" && exit 1)
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test --project=chromium --grep "$(GREP)"

test-perf: ## Run performance evaluation tests (chat latency profiling)
	@echo "🧪 Running performance evaluation tests..."
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test --project=chromium tests/chat_performance_eval.spec.ts
	@echo "✅ Performance tests complete. Metrics exported to apps/zerg/e2e/metrics/"

test-zerg-unit: ## Run Zerg unit tests (backend + frontend)
	@if [ "$(MINIMAL)" = "1" ]; then \
		echo "🧪 Running Zerg unit tests (minimal)..." && \
		(cd apps/zerg/backend && ./run_backend_tests.sh -q --no-header) && \
		(cd apps/zerg/frontend-web && bun run test -- --reporter=dot --silent); \
	else \
		echo "🧪 Running Zerg unit tests..." && \
		(cd apps/zerg/backend && ./run_backend_tests.sh) && \
		(cd apps/zerg/frontend-web && bun run test); \
	fi

test-frontend-unit: ## Run frontend unit tests only
	@if [ "$(MINIMAL)" = "1" ]; then \
		echo "🧪 Running frontend unit tests (minimal)..."; \
		cd apps/zerg/frontend-web && bun run test -- --reporter=dot --silent; \
	else \
		echo "🧪 Running frontend unit tests..."; \
		cd apps/zerg/frontend-web && bun run test; \
	fi

test-integration: ## Run integration tests (REAL API calls, requires API keys)
	@echo "🧪 Running integration tests (real API calls)..."
	@echo "   Note: Requires OPENAI_API_KEY and/or GROQ_API_KEY"
	cd apps/zerg/backend && EVAL_MODE=live uv run pytest tests/integration/ -v -m integration

test-zerg-e2e: ## Run Zerg E2E tests (Playwright)
	@echo "🧪 Running Zerg E2E tests..."
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test --project=chromium

test-prompts: ## Run live prompt quality tests (requires backend running + --live-token)
	@echo "🧪 Running prompt quality tests (requires backend running)..."
	@echo "   Example: make test-prompts TOKEN=your-jwt-token"
	@if [ -z "$(TOKEN)" ]; then \
		echo "❌ Missing TOKEN. Usage: make test-prompts TOKEN=<jwt-token>"; \
		exit 1; \
	fi
	@echo "   Using http://localhost:30080 (dev nginx entry)"
	@echo "   Setting LLM_REQUEST_LOG=1 for debugging..."
	cd apps/zerg/backend && \
		LLM_REQUEST_LOG=1 \
		SWARMLET_DATA_PATH=$${SWARMLET_DATA_PATH:-/tmp/swarmlet} \
		uv run pytest tests/live/test_prompt_quality.py \
		--live-url http://localhost:30080 \
		--live-token $(TOKEN) \
		--timeout=120 \
		-v

# ---------------------------------------------------------------------------
# Eval Tests (AI Agent Evaluation)
# ---------------------------------------------------------------------------
EVAL_VARIANT ?= baseline

eval: ## Run eval tests (hermetic mode, baseline variant)
	@echo "🧪 Running eval tests (hermetic mode)..."
	cd apps/zerg/backend && uv run pytest evals/ -v -n auto --variant=$(EVAL_VARIANT) --timeout=60

eval-live: ## Run eval tests (LIVE mode - real OpenAI)
	@echo "🔴 Running eval tests (LIVE mode - real OpenAI)..."
	cd apps/zerg/backend && env EVAL_MODE=live uv run pytest evals/ -v --variant=$(EVAL_VARIANT) --timeout=120

eval-compare: ## Compare two eval result files (usage: make eval-compare BASELINE=file1 VARIANT=file2)
	@test -n "$(BASELINE)" || (echo "❌ Usage: make eval-compare BASELINE=<file> VARIANT=<file>" && exit 1)
	@test -n "$(VARIANT)" || (echo "❌ Usage: make eval-compare BASELINE=<file> VARIANT=<file>" && exit 1)
	@echo "📊 Comparing eval results..."
	cd apps/zerg/backend && uv run python -m evals.compare evals/results/$(BASELINE) evals/results/$(VARIANT)

eval-critical: ## Run critical tests only (deployment gate - must pass 100%)
	@echo "🔴 Running CRITICAL eval tests (deployment gate)..."
	@echo "⚠️  These tests MUST pass 100% for deployment"
	cd apps/zerg/backend && uv run pytest evals/ -v -n auto -m critical --variant=$(EVAL_VARIANT) --timeout=60

eval-fast: ## Run fast tests only (quick sanity check)
	@echo "⚡ Running FAST eval tests (quick sanity check)..."
	cd apps/zerg/backend && uv run pytest evals/ -v -n auto -m fast --variant=$(EVAL_VARIANT) --timeout=30

eval-all: ## Run all eval tests including slow ones
	@echo "🔬 Running ALL eval tests (including slow tests)..."
	cd apps/zerg/backend && uv run pytest evals/ -v -n auto --variant=$(EVAL_VARIANT) --timeout=120

eval-tool-selection: ## Run tool selection evals (LIVE mode - tests tool picking quality)
	@echo "🎯 Running tool selection evals (LIVE mode)..."
	cd apps/zerg/backend && env EVAL_MODE=live uv run pytest evals/ -v -k tool_selection --timeout=120

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

seed-marketing: ## Seed marketing data (workflows, agents, chat thread)
	@echo "🌱 Seeding marketing data..."
	@BACKEND=$$(docker ps --format "{{.Names}}" | grep "backend" | head -1); \
	if [ -z "$$BACKEND" ]; then \
		echo "❌ Backend not running. Start with 'make dev'"; \
		exit 1; \
	fi; \
	docker exec $$BACKEND uv run python scripts/seed_marketing_workflow.py

marketing-capture: ## Capture all marketing screenshots (manifest-driven)
	@echo "📸 Capturing marketing screenshots..."
	@if ! curl -sf http://localhost:30080/health >/dev/null 2>&1; then \
		echo "❌ Dev stack not running. Start with 'make dev'"; \
		exit 1; \
	fi
	$(MAKE) seed-marketing
	@uv run --with playwright --with pyyaml scripts/capture_marketing.py

marketing-single: ## Capture specific screenshot (usage: make marketing-single NAME=chat-preview)
	@test -n "$(NAME)" || (echo "❌ Usage: make marketing-single NAME=<screenshot-name>" && exit 1)
	@if ! curl -sf http://localhost:30080/health >/dev/null 2>&1; then \
		echo "❌ Dev stack not running. Start with 'make dev'"; \
		exit 1; \
	fi
	@uv run --with playwright --with pyyaml scripts/capture_marketing.py --name $(NAME)

marketing-validate: ## Validate all marketing screenshots exist and have reasonable size
	@uv run --with pyyaml scripts/capture_marketing.py --validate

marketing-list: ## List available marketing screenshots
	@uv run --with pyyaml scripts/capture_marketing.py --list

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
	@printf '\n4️⃣  Checking for test anti-patterns...\n'
	@$(MAKE) lint-test-patterns
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

lint-test-patterns: ## Check for test anti-patterns (window.confirm, alert, waitForTimeout)
	@bash scripts/lint-test-patterns.sh

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

# ---------------------------------------------------------------------------
# LangGraph Debug Commands (AI-optimized, minimal tokens)
# ---------------------------------------------------------------------------
debug-thread: ## Inspect DB ThreadMessages (usage: make debug-thread THREAD_ID=1)
	@test -n "$(THREAD_ID)" || (echo "❌ Usage: make debug-thread THREAD_ID=<id>" && exit 1)
	@cd apps/zerg/backend && uv run python scripts/debug_langgraph.py thread $(THREAD_ID)

debug-validate: ## Validate message integrity (usage: make debug-validate THREAD_ID=1)
	@test -n "$(THREAD_ID)" || (echo "❌ Usage: make debug-validate THREAD_ID=<id>" && exit 1)
	@cd apps/zerg/backend && uv run python scripts/debug_langgraph.py validate $(THREAD_ID)

debug-inspect: ## Inspect LangGraph checkpoint state (usage: make debug-inspect THREAD_ID=1)
	@test -n "$(THREAD_ID)" || (echo "❌ Usage: make debug-inspect THREAD_ID=<id>" && exit 1)
	@cd apps/zerg/backend && uv run python scripts/debug_langgraph.py inspect $(THREAD_ID)

debug-batch: ## Run batch queries from stdin JSON (usage: echo '{"queries":[...]}' | make debug-batch)
	@cd apps/zerg/backend && uv run python scripts/debug_langgraph.py batch --stdin

debug-trace: ## Debug a trace end-to-end (usage: make debug-trace TRACE=abc-123 or make debug-trace RECENT=1)
	@if [ -n "$(RECENT)" ]; then \
		cd apps/zerg/backend && uv run python scripts/debug_trace.py --recent; \
	elif [ -n "$(TRACE)" ]; then \
		cd apps/zerg/backend && uv run python scripts/debug_trace.py $(TRACE) $(if $(LEVEL),--level $(LEVEL),); \
	else \
		echo "❌ Usage: make debug-trace TRACE=<uuid> [LEVEL=summary|full|errors]"; \
		echo "         make debug-trace RECENT=1"; \
		exit 1; \
	fi

trace-coverage: ## Trace coverage report (usage: make trace-coverage [SINCE_HOURS=24] [MIN=95] [MIN_EVENTS=90] [JSON=1])
	@cd apps/zerg/backend && uv run python scripts/trace_coverage.py \
		$(if $(SINCE_HOURS),--since-hours $(SINCE_HOURS),) \
		$(if $(MIN),--min-percent $(MIN),) \
		$(if $(MIN_EVENTS),--min-event-percent $(MIN_EVENTS),) \
		$(if $(JSON),--json,) \
		$(if $(NO_EVENTS),--no-events,)
