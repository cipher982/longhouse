# Swarm Platform (Oikos + Zerg Monorepo)

# ---------------------------------------------------------------------------
# Load environment variables from .env
# ---------------------------------------------------------------------------
-include .env
export $(shell sed 's/=.*//' .env 2>/dev/null || true)

# Compose helpers (keep flags consistent across targets)
COMPOSE_DEV := docker compose --project-name zerg --env-file .env -f docker/docker-compose.dev.yml

.PHONY: help dev dev-demo demo-db stop dev-docker dev-docker-bg stop-docker logs logs-app logs-db doctor dev-reset-db reset test test-readmes test-lite test-autonomy-journeys run-autonomy-journeys ensure-js-deps test-control-plane test-e2e-cp test-integration test-e2e test-e2e-core test-full test-chat-e2e test-e2e-single test-e2e-continuation-provider test-e2e-ui test-e2e-verbose test-e2e-errors test-e2e-query test-e2e-grep test-e2e-a11y test-e2e-onboarding qa-ui qa-ui-visual qa-ui-smoke qa-ui-smoke-update qa-ui-baseline qa-ui-baseline-update qa-ui-baseline-mobile qa-ui-baseline-mobile-update qa-ui-full qa-oss qa-live qa-live-conversations qa-visual-compare qa-visual-compare-fast test-perf test-zerg-ops-backup test-frontend-unit test-hatch-agent test-runner-unit test-install-runner test-runner-vm-canary test-install test-install-first-run test-install-remote test-provision-e2e test-prompts test-ci test-shipper-e2e shipper-e2e-prereqs shipper-smoke-test test-hooks eval eval-compare eval-tool-selection generate-sdk seed-agents seed-credentials marketing-screenshots marketing-validate marketing-list validate validate-ws regen-ws validate-sse regen-sse validate-makefile lint-test-patterns env-check env-check-prod verify-prod perf-landing perf-gpu perf-gpu-dashboard debug-thread debug-validate debug-inspect debug-batch debug-trace trace-coverage onboarding-funnel onboarding-smoke onboarding-sqlite launch-gate-local ui-capture video-studio video-remotion video-remotion-web video-remotion-preview vibetest vibetest-local install-engine test-engine-fast test-shipper-premerge


# ---------------------------------------------------------------------------
# Help – `make` or `make help` (auto-generated from ## comments)
# ---------------------------------------------------------------------------
help: ## Show this help message
	@echo "\n🌐 Swarm Platform (Oikos + Zerg)"
	@echo "=================================="
	@echo ""
	@grep -B0 '## ' Makefile | grep -E '^[a-zA-Z0-9_-]+:' | grep -v '## @internal' | sed 's/:.*## /: /' | column -t -s ':' | awk '{printf "  %-24s %s\n", $$1":", substr($$0, index($$0,$$2))}' | sort
	@echo ""

# ---------------------------------------------------------------------------
# Environment Validation
# ---------------------------------------------------------------------------
env-check: ## Validate required environment variables (Docker mode)
	@missing=0; \
	warn=0; \
	echo "🔍 Checking Docker environment variables..."; \
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
dev: ## ⭐ Start development environment (SQLite native, no Docker)
	@echo "🚀 Starting development environment (SQLite)..."
	@env -u DATABASE_URL ./scripts/dev.sh

dev-demo: ## Start demo environment (seeded SQLite DB)
	@echo "Starting demo environment (SQLite demo DB)..."
	@env -u DATABASE_URL ./scripts/dev-demo.sh

demo-db: ## Build demo SQLite database
	@uv run python apps/zerg/backend/scripts/build_demo_db.py

stop: ## Stop development services
	@echo "Stopping development services..."
	@pkill -f "uvicorn zerg.main:app" 2>/dev/null || true
	@pkill -f "vite" 2>/dev/null || true
	@echo "✅ Stopped"

# Legacy Docker targets (for CI or Postgres-specific testing)
dev-docker: env-check ## Start Docker development environment (legacy)
	@echo "🚀 Starting Docker environment..."
	@./scripts/dev-docker.sh

dev-docker-bg: env-check ## Start Docker environment in background (legacy)
	@echo "🚀 Starting Docker environment (background)..."
	$(COMPOSE_DEV) --profile dev up -d --build --wait
	$(COMPOSE_DEV) --profile dev ps

stop-docker: ## Stop Docker services
	@./scripts/stop-docker.sh

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

# =============================================================================
# EXPLICIT TEST TIERS (long names for agents; keep legacy targets for scripts)
# =============================================================================

# =============================================================================

# Playwright E2E should run against an isolated frontend/backend pair by default.
# Ports are randomized unless explicitly pinned:
#   make test-e2e E2E_BACKEND_PORT=47300 E2E_FRONTEND_PORT=47200
E2E_BACKEND_PORT ?=
E2E_FRONTEND_PORT ?=
SHIPPER_E2E_URL ?= http://localhost:47300

test: ## Run lite tests by default
	$(MAKE) test-lite

test-readmes: ## Run README contract tests (MODE=smoke[default] or full)
	@python3 scripts/run-readme-tests.py --mode $(or $(MODE),smoke) $(FILES)

test-lite: ## Fast SQLite-lite backend tests (no Docker)
	@echo "🧪 Running lite backend tests (SQLite)..."; \
	(cd apps/zerg/backend && ./run_backend_tests_lite.sh)
	@$(MAKE) test-control-plane
	@$(MAKE) test-engine-fast

test-autonomy-journeys: ## Run deterministic Oikos autonomy journey harness tests
	@echo "🧪 Running Oikos autonomy journey harness tests..."
	cd apps/zerg/backend && ./run_backend_tests_lite.sh tests_lite/test_oikos_autonomy_journeys.py

run-autonomy-journeys: ## Run Oikos autonomy journey fixtures and save artifacts to .tmp/
	@echo "🧪 Running Oikos autonomy journeys with durable artifacts..."
	cd apps/zerg/backend && uv run python scripts/run_oikos_autonomy_journeys.py

test-control-plane: ## Fast control-plane unit tests (no Docker)
	@echo "🧪 Running control-plane tests..."; \
	cd apps/control-plane && \
	uv sync --extra dev --frozen >/dev/null && \
	uv run --extra dev pytest tests -q

test-e2e-cp: ## Control plane E2E (Playwright, local server, no Docker)
	@echo "🎭 Running control-plane E2E tests..."; \
	cd apps/control-plane && \
	uv sync --extra dev --frozen >/dev/null && \
	uv run --extra dev playwright install chromium --with-deps >/dev/null 2>&1 || true && \
	uv run --extra dev pytest e2e/ -v

install-engine: ## Build + sign the Rust engine binary (run after any engine source change)
	cd apps/engine && cargo build --release
	codesign -s - apps/engine/target/release/longhouse-engine
	@echo "longhouse-engine installed (symlink at ~/.local/bin/longhouse-engine)"

test-engine-fast: ## Rust engine unit + golden + adversarial tests (uses repo-local binary, included in make test)
	@echo "🦀 Running engine unit + golden + adversarial tests..."
	cd apps/engine && cargo build --release
	cd apps/engine && cargo test --bin longhouse-engine --test golden_parser_contract --test adversarial_parser

test-zerg-ops-backup: ## Backup/restore retention contract test for scripts/zerg-ops.sh
	@bash scripts/test-zerg-ops.sh

test-shipper-e2e: ## Full pipeline E2E: fixture → longhouse-engine ship → API → DB (uses repo-local binary)
	@echo "🚀 Running shipper E2E tests (Claude/Gemini/Codex + schema-drift)..."
	@echo "🦀 Building release engine binary (avoids stale-binary false confidence)..."
	cd apps/engine && cargo build --release
	cd apps/zerg/backend && uv run --extra dev pytest tests/integration/test_shipper_e2e.py -m integration -v

test-shipper-premerge: ## Full shipper QA: engine fast tests + pipeline E2E (run before merging engine changes)
	$(MAKE) test-engine-fast
	$(MAKE) test-shipper-e2e

ensure-js-deps: ## @internal Install workspace JS deps when a clean clone needs them
	@if [ ! -f node_modules/@playwright/test/package.json ]; then \
		echo "📦 Installing JS workspace dependencies..."; \
		bun install --frozen-lockfile; \
	fi

test-e2e: ## Run E2E tests (core + a11y)
	@echo "🎭 Running E2E tests (core + a11y)..."
	$(MAKE) test-e2e-core
	$(MAKE) test-e2e-a11y

test-e2e-core: ## @internal Run core E2E tests only (no retries, must pass 100%)
	@$(MAKE) ensure-js-deps
	@echo "🔴 Running CORE E2E tests (no retries, must pass 100%)..."
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) \
		bunx playwright test --project=core --retries=0 --workers=1

test-full: ## Full local suite (unit + full E2E + visual baselines + visual compare)
	@echo "🧪 Running full local suite (unit + full E2E + visual baselines + visual compare)..."
	$(MAKE) test
	$(MAKE) test-e2e-core
	$(MAKE) test-e2e-a11y
	$(MAKE) qa-ui-baseline
	$(MAKE) qa-ui-baseline-mobile
	$(MAKE) qa-visual-compare-fast

test-chat-e2e: ## Run Oikos chat E2E tests (inside unified SPA)
	@$(MAKE) ensure-js-deps
	@echo "🧪 Running chat E2E tests (unified SPA)..."
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test --project=chromium tests/unified-frontend.spec.ts

test-e2e-single: ## @internal Run a single E2E test (usage: make test-e2e-single TEST=tests/unified-frontend.spec.ts)
	@$(MAKE) ensure-js-deps
	@test -n "$(TEST)" || (echo "❌ Usage: make test-e2e-single TEST=<spec-or-args>" && exit 1)
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test $(TEST)

test-e2e-continuation-provider: ## Run the real provider-backed continuation smoke (requires ANTHROPIC_API_KEY + claude CLI; optional PROVIDER_SMOKE_ARTIFACT_DIR)
	@$(MAKE) ensure-js-deps
	cd apps/zerg/frontend-web && bun run build
	cd apps/zerg/e2e && E2E_BACKEND_PORT=$(E2E_BACKEND_PORT) node scripts/provider-continuation-smoke.mjs

test-e2e-ui: ## @internal Run Playwright E2E tests with interactive UI
	@$(MAKE) ensure-js-deps
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test --project=chromium --ui

test-e2e-verbose: ## @internal Run E2E tests with full verbose output (for debugging)
	@$(MAKE) ensure-js-deps
	cd apps/zerg/e2e && VERBOSE=1 BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test --project=chromium

test-e2e-errors: ## @internal Show detailed errors from last E2E run
	@if [ -f apps/zerg/e2e/test-results/errors.txt ]; then \
		cat apps/zerg/e2e/test-results/errors.txt; \
	else \
		echo "No errors.txt found. Run 'make test-e2e' first."; \
	fi

test-e2e-query: ## @internal Query last E2E results (usage: make test-e2e-query Q='.failed[]')
	@if [ -f apps/zerg/e2e/test-results/summary.json ]; then \
		jq '$(Q)' apps/zerg/e2e/test-results/summary.json; \
	else \
		echo "No summary.json found. Run 'make test-e2e' first."; \
	fi

test-e2e-grep: ## @internal Run E2E tests by name (usage: make test-e2e-grep GREP="test name")
	@$(MAKE) ensure-js-deps
	@test -n "$(GREP)" || (echo "❌ Usage: make test-e2e-grep GREP='test name'" && exit 1)
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test --project=chromium --grep "$(GREP)"

test-e2e-a11y: ## @internal Run accessibility UI/UX checks (axe + heuristics)
	@$(MAKE) ensure-js-deps
	@echo "🧪 Running accessibility UI/UX checks..."
	cd apps/zerg/e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test --project=chromium tests/accessibility.spec.ts

qa-ui: ## Quick UI QA (accessibility checks)
	$(MAKE) test-e2e-a11y

qa-ui-visual: ## Visual UI analysis (screenshots + AI) (usage: make qa-ui-visual ARGS="--pages=dashboard,chat")
	@./scripts/run-visual-analysis.sh $(ARGS)

qa-ui-smoke: ## Visual smoke snapshots for core app pages (glass)
	$(MAKE) test-e2e-single TEST="--project=chromium tests/visual_smoke_glass.spec.ts"

qa-ui-smoke-update: ## @internal Update visual smoke snapshots for core app pages (glass)
	$(MAKE) test-e2e-single TEST="--project=chromium --update-snapshots tests/visual_smoke_glass.spec.ts"

qa-ui-baseline: ## Visual baselines for app + public pages
	$(MAKE) test-e2e-single TEST=tests/ui_baseline_public.spec.ts
	$(MAKE) test-e2e-single TEST=tests/ui_baseline_app.spec.ts

qa-ui-baseline-update: ## @internal Update visual baselines for app + public pages
	$(MAKE) test-e2e-single TEST="--update-snapshots=all tests/ui_baseline_public.spec.ts"
	$(MAKE) test-e2e-single TEST="--update-snapshots=all tests/ui_baseline_app.spec.ts"

qa-ui-baseline-mobile: ## Visual baselines for mobile viewport pages
	$(MAKE) test-e2e-single TEST="--project=mobile tests/mobile/ui_baseline_mobile.spec.ts"
	$(MAKE) test-e2e-single TEST="--project=mobile-small tests/mobile/ui_baseline_mobile.spec.ts"

qa-ui-baseline-mobile-update: ## @internal Update visual baselines for mobile viewport pages
	$(MAKE) test-e2e-single TEST="--project=mobile --update-snapshots=all tests/mobile/ui_baseline_mobile.spec.ts"
	$(MAKE) test-e2e-single TEST="--project=mobile-small --update-snapshots=all tests/mobile/ui_baseline_mobile.spec.ts"

qa-visual-compare: ## Visual comparison with LLM triage (catches color/layout catastrophes)
	$(MAKE) test-e2e-single TEST="--project=chromium tests/visual_compare.spec.ts"

qa-visual-compare-fast: ## Visual comparison without LLM (pixelmatch only, faster)
	SKIP_LLM=1 $(MAKE) test-e2e-single TEST="--project=chromium tests/visual_compare.spec.ts"

qa-ui-full: ## Full UI regression sweep (a11y + desktop + mobile baselines + visual compare)
	$(MAKE) qa-ui
	$(MAKE) qa-ui-baseline
	$(MAKE) qa-ui-baseline-mobile
	$(MAKE) qa-visual-compare-fast

test-perf: ## Run performance evaluation tests (chat latency profiling)
	@$(MAKE) ensure-js-deps
	@echo "🧪 Running performance evaluation tests..."
	cd apps/zerg/e2e && RUN_PERF=1 BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test --project=chromium tests/chat_performance_eval.spec.ts
	@echo "✅ Performance tests complete. Metrics exported to apps/zerg/e2e/metrics/"

test-frontend-unit: ## @internal Run frontend unit tests only
	@if [ "$(MINIMAL)" = "1" ]; then \
		echo "🧪 Running frontend unit tests (minimal)..."; \
		cd apps/zerg/frontend-web && bun run test -- --reporter=dot --silent; \
	else \
		echo "🧪 Running frontend unit tests..."; \
		cd apps/zerg/frontend-web && bun run test; \
	fi

test-hatch-agent: ## @internal Run hatch-agent package tests from sibling repo
	@if [ ! -d ../hatch ]; then \
		echo "❌ hatch repo not found at ../hatch"; \
		echo "Clone it with: gh repo clone cipher982/hatch ../hatch"; \
		exit 1; \
	fi
	@if [ "$(MINIMAL)" = "1" ]; then \
		echo "🧪 Running hatch-agent tests (minimal)..."; \
		cd ../hatch && uv run --extra dev pytest tests/ --ignore=tests/test_integration.py -q; \
	else \
		echo "🧪 Running hatch-agent tests..."; \
		cd ../hatch && uv run --extra dev pytest tests/ --ignore=tests/test_integration.py; \
	fi

test-runner-unit: ## @internal Run runner unit tests
	@echo "🧪 Running runner unit tests..."
	cd apps/runner && bun test

test-install-runner: ## @internal Run install-runner script tests
	@echo "🧪 Running install-runner script tests..."
	bash scripts/tests/install-runner.test.sh

test-runner-vm-canary: ## Run disposable VM runner canary against a hosted instance
	@echo "🧪 Running disposable VM runner canary..."
	bash scripts/runner-vm-canary.sh

test-install: ## Test Longhouse installer + first-run onboarding smoke
	@echo "🧪 Testing Longhouse installer..."
	@bash -n scripts/install.sh
	@echo "✅ Syntax OK"
	@$(MAKE) test-install-first-run

test-install-first-run: ## Run disposable first-run installer smoke in a temp HOME
	@./scripts/ci/installer-first-run.sh

test-install-remote: ## Run public installer smoke against get.longhouse.ai (manual/scheduled)
	@./scripts/ci/installer-first-run.sh --installer remote

launch-gate-local: test-install onboarding-funnel ## Run the local launch onboarding gate

test-provision-e2e: ## Provision an instance via control plane and run smoke checks
	@./scripts/ci/provision-e2e.sh

test-integration: ## @internal Run integration tests (REAL API calls, requires API keys)
	@echo "🧪 Running integration tests (real API calls)..."
	@echo "   Note: Requires OPENAI_API_KEY and/or GROQ_API_KEY"
	cd apps/zerg/backend && EVAL_MODE=live uv run --extra dev pytest tests/integration/ -v -m integration

shipper-e2e-prereqs: ## Shipper E2E prerequisites (migrations + table check)
	@./scripts/shipper-e2e-prereqs.sh

## Note: test-shipper-e2e is defined earlier in this file (self-contained, no make dev required)

shipper-smoke-test: ## Run shipper live smoke test script (requires backend running)
	@./scripts/shipper-smoke-test.sh

test-hooks: ## E2E test for hook outbox pipeline (requires daemon running)
	@./scripts/test-hooks-e2e.sh

test-prompts: ## @internal Run live prompt quality tests (requires backend running + --live-token)
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
		LONGHOUSE_DATA_PATH=$${LONGHOUSE_DATA_PATH:-/tmp/longhouse} \
		uv run --extra dev pytest tests/live/test_prompt_quality.py \
		--live-url http://localhost:30080 \
		--live-token $(TOKEN) \
		--timeout=120 \
		-v

# ---------------------------------------------------------------------------
# Eval Tests (AI Quality - REAL LLM calls, costs money)
# ---------------------------------------------------------------------------
# NOTE: Evals use REAL OpenAI API calls. Not run in CI.
# Use for manual quality testing or scheduled jobs.
EVAL_VARIANT ?= baseline

eval: ## 🔴 Run AI evals (REAL LLM - costs $$$)
	@echo "🔴 Running AI evals (REAL OpenAI API calls)..."
	@echo "   This costs money. Press Ctrl+C to cancel."
	@sleep 2
	cd apps/zerg/backend && env EVAL_MODE=live uv run --extra dev pytest evals/ -v --variant=$(EVAL_VARIANT) --timeout=120

eval-compare: ## @internal Compare two eval result files (usage: make eval-compare BASELINE=file1 VARIANT=file2)
	@test -n "$(BASELINE)" || (echo "❌ Usage: make eval-compare BASELINE=<file> VARIANT=<file>" && exit 1)
	@test -n "$(VARIANT)" || (echo "❌ Usage: make eval-compare BASELINE=<file> VARIANT=<file>" && exit 1)
	@echo "📊 Comparing eval results..."
	cd apps/zerg/backend && uv run python -m evals.compare evals/results/$(BASELINE) evals/results/$(VARIANT)

eval-tool-selection: ## @internal Run tool selection evals (tests tool picking quality)
	@echo "🎯 Running tool selection evals (REAL LLM)..."
	cd apps/zerg/backend && env EVAL_MODE=live uv run --extra dev pytest evals/ -v -k tool_selection --timeout=120

# ---------------------------------------------------------------------------
# SDK & Integration
# ---------------------------------------------------------------------------
generate-sdk: ## Generate OpenAPI types from backend schema
	@echo "🔄 Generating SDK..."
	@$(MAKE) ensure-js-deps
	@cd apps/zerg/backend && uv run python scripts/export_openapi.py >/dev/null
	@cd apps/zerg/frontend-web && bun run openapi-typescript ../openapi.json --output src/generated/openapi-types.ts
	@echo "✅ SDK generation complete"

seed-agents: ## Seed baseline Zerg agents for Oikos
	@echo "🌱 Seeding agents..."
	@BACKEND=$$(docker ps --format "{{.Names}}" | grep "backend" | head -1); \
	if [ -z "$$BACKEND" ]; then \
		echo "❌ Backend not running. Start with 'make dev'"; \
		exit 1; \
	fi
	@docker exec $$BACKEND uv run python scripts/seed_oikos_agents.py
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

marketing-screenshots: ## Capture marketing screenshots (self-contained: starts+stops dev stack automatically)
	@echo "📸 Capturing marketing screenshots..."
	@bash scripts/marketing-screenshots.sh $(NAME)

marketing-validate: ## Validate all marketing screenshots exist and have reasonable size
	@uv run --with pyyaml scripts/capture_marketing.py --validate

marketing-list: ## List available marketing screenshots
	@uv run --with pyyaml scripts/capture_marketing.py --list

# ---------------------------------------------------------------------------
# Video Generation (Remotion)
# Canonical pipeline: make video-remotion-web
# ---------------------------------------------------------------------------
SCENARIO ?= product-demo
.PHONY: video-audio video-studio video-remotion video-remotion-web video-remotion-preview

video-audio: ## Generate voiceover audio. Override: SCENARIO=timeline-demo
	@echo "🎙️  Generating voiceover audio for $(SCENARIO)..."
	@uv run --with openai --with mutagen --with pyyaml scripts/generate_voiceover.py $(SCENARIO)
	@echo "✅ Audio generated. Check videos/$(SCENARIO)/audio/"

video-studio: ## Open Remotion Studio for video editing
	@cd apps/video && bunx remotion studio

video-remotion: ## Render timeline demo via Remotion
	@echo "🎬 Rendering TimelineDemo via Remotion..."
	@cd apps/video && bunx remotion render TimelineDemo out/timeline-demo.mp4 --codec h264 --crf 18
	@echo "✅ Rendered: apps/video/out/timeline-demo.mp4"

video-remotion-web: ## Render + compress for web (CRF 23)
	@echo "🎬 Rendering TimelineDemo for web..."
	@cd apps/video && bunx remotion render TimelineDemo out/timeline-demo.mp4 --codec h264 --crf 23
	@cp apps/video/out/timeline-demo.mp4 apps/zerg/frontend-web/public/videos/timeline-demo.mp4
	@echo "✅ Copied to frontend public/videos/"

video-remotion-preview: ## Render single frame for quick preview
	@cd apps/video && bunx remotion still TimelineDemo out/preview.jpg --frame 150
	@echo "✅ Preview: apps/video/out/preview.jpg"

# ---------------------------------------------------------------------------
# UI Capture (Debug Bundles for Agents)
# ---------------------------------------------------------------------------
ui-capture: ## Capture local dev UI debug bundle (requires dev running)
	@bunx tsx scripts/ui-capture.ts $(PAGE) $(if $(SCENE),--scene=$(SCENE),) $(if $(OUTPUT),--output=$(OUTPUT),) $(if $(ALL),--all,) $(if $(NO_TRACE),--no-trace,)

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
	@cd apps/zerg/backend && \
		export XDG_CACHE_HOME="$$PWD/.uv_cache" TMPDIR="$$PWD/.uv_tmp"; \
		mkdir -p "$$XDG_CACHE_HOME" "$$TMPDIR"; \
		uv run --no-project --with pyyaml python ../../../scripts/generate-ws-types-modern.py schemas/ws-protocol-asyncapi.yml >/dev/null 2>&1
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
	@cd apps/zerg/backend && \
		export XDG_CACHE_HOME="$$PWD/.uv_cache" TMPDIR="$$PWD/.uv_tmp"; \
		mkdir -p "$$XDG_CACHE_HOME" "$$TMPDIR"; \
		uv run --no-project --with pyyaml python ../../../scripts/generate-ws-types-modern.py schemas/ws-protocol-asyncapi.yml
	@echo "✅ WebSocket code regenerated"

validate-sse: ## Check SSE code is in sync (for CI)
	@cd apps/zerg/backend && \
		export XDG_CACHE_HOME="$$PWD/.uv_cache" TMPDIR="$$PWD/.uv_tmp"; \
		mkdir -p "$$XDG_CACHE_HOME" "$$TMPDIR"; \
		uv run --no-project --with pyyaml python ../../../scripts/generate-sse-types.py schemas/sse-events.asyncapi.yml >/dev/null 2>&1
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
	@cd apps/zerg/backend && \
		export XDG_CACHE_HOME="$$PWD/.uv_cache" TMPDIR="$$PWD/.uv_tmp"; \
		mkdir -p "$$XDG_CACHE_HOME" "$$TMPDIR"; \
		uv run --no-project --with pyyaml python ../../../scripts/generate-sse-types.py schemas/sse-events.asyncapi.yml
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
# Production Verification
# ---------------------------------------------------------------------------
verify-prod: ## Full prod validation: API + browser tests (~80s, requires hosted auth env)
	@echo "🔍 Verifying production..."
	@./scripts/smoke-prod.sh --wait --full
	@echo ""
	@./scripts/run-prod-e2e.sh
	@echo ""
	@./scripts/check-cp-credentials.sh
	@echo "✅ Production verified"

qa-live: ## Run live QA against hosted instance (~60s, default subdomain david010)
	@./scripts/qa-live.sh

qa-live-conversations: ## Run hosted conversations smoke against default instance
	@./scripts/run-prod-e2e.sh tests/live/conversations-live.spec.ts --timeout=60000 --reporter=line

test-ci: ## @internal CI-ready tests (unit + build + contracts)
	@echo "🤖 CI Test Suite Starting..."
	@echo "═══════════════════════════════════════════════════════════════════════════════"
	@echo "🧪 Running React Unit Tests..."
	@cd apps/zerg/frontend-web && bun run test -- --run --reporter=basic
	@echo "  ✅ React unit tests passed"
	@echo ""
	@echo "🏗️  Testing React Build..."
	@cd apps/zerg/frontend-web && bun run build >/dev/null 2>&1
	@echo "  ✅ React build successful"
	@echo ""
	@echo "🧪 Running Backend Lite Tests..."
	@cd apps/zerg/backend && ./run_backend_tests_lite.sh >/dev/null
	@echo "  ✅ Backend lite tests passed"
	@echo ""
	@echo "🔍 Running Contract Validation..."
	@cd apps/zerg/frontend-web && bun run validate:contracts >/dev/null
	@echo "  ✅ API contracts valid"
	@echo ""
	@echo "═══════════════════════════════════════════════════════════════════════════════"
	@echo "🎯 CI Test Summary:"
	@echo "  ✓ React unit tests"
	@echo "  ✓ React build process"
	@echo "  ✓ Backend lite tests"
	@echo "  ✓ API contract validation"
	@echo ""
	@echo "✨ All CI checks passed! Ready for deployment."
	@echo "═══════════════════════════════════════════════════════════════════════════════"

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
debug-thread: ## @internal Inspect DB ThreadMessages (usage: make debug-thread THREAD_ID=1)
	@test -n "$(THREAD_ID)" || (echo "❌ Usage: make debug-thread THREAD_ID=<id>" && exit 1)
	@cd apps/zerg/backend && uv run python scripts/debug_langgraph.py thread $(THREAD_ID)

debug-validate: ## @internal Validate message integrity (usage: make debug-validate THREAD_ID=1)
	@test -n "$(THREAD_ID)" || (echo "❌ Usage: make debug-validate THREAD_ID=<id>" && exit 1)
	@cd apps/zerg/backend && uv run python scripts/debug_langgraph.py validate $(THREAD_ID)

debug-inspect: ## @internal Inspect LangGraph checkpoint state (usage: make debug-inspect THREAD_ID=1)
	@test -n "$(THREAD_ID)" || (echo "❌ Usage: make debug-inspect THREAD_ID=<id>" && exit 1)
	@cd apps/zerg/backend && uv run python scripts/debug_langgraph.py inspect $(THREAD_ID)

debug-batch: ## @internal Run batch queries from stdin JSON (usage: echo '{"queries":[...]}' | make debug-batch)
	@cd apps/zerg/backend && uv run python scripts/debug_langgraph.py batch --stdin

debug-trace: ## @internal Debug a trace end-to-end (usage: make debug-trace TRACE=abc-123 or make debug-trace RECENT=1)
	@if [ -n "$(RECENT)" ]; then \
		cd apps/zerg/backend && uv run python scripts/debug_trace.py --recent; \
	elif [ -n "$(TRACE)" ]; then \
		cd apps/zerg/backend && uv run python scripts/debug_trace.py $(TRACE) $(if $(LEVEL),--level $(LEVEL),); \
	else \
		echo "❌ Usage: make debug-trace TRACE=<uuid> [LEVEL=summary|full|errors]"; \
		echo "         make debug-trace RECENT=1"; \
		exit 1; \
	fi

trace-coverage: ## @internal Trace coverage report (usage: make trace-coverage [SINCE_HOURS=24] [MIN=95] [MIN_EVENTS=90] [JSON=1])
	@cd apps/zerg/backend && uv run python scripts/trace_coverage.py \
		$(if $(SINCE_HOURS),--since-hours $(SINCE_HOURS),) \
		$(if $(MIN),--min-percent $(MIN),) \
		$(if $(MIN_EVENTS),--min-event-percent $(MIN_EVENTS),) \
		$(if $(JSON),--json,) \
		$(if $(NO_EVENTS),--no-events,)

test-e2e-onboarding: ## @internal Run onboarding browser ring (Playwright + demo server)
	@echo "🎭 Running onboarding browser ring..."
	@ONBOARDING_PLAYWRIGHT_PROJECT="$(PROJECT)" ./scripts/qa-oss.sh --workdir $(CURDIR) --no-unit --no-e2e

# ---------------------------------------------------------------------------
# Onboarding Funnel (docs-as-source)
# ---------------------------------------------------------------------------
onboarding-funnel: ## Run onboarding funnel from README contract (fresh clone)
	@./scripts/run-onboarding-funnel.sh

onboarding-smoke: ## Quick onboarding smoke (uses current workspace, Docker)
	@./scripts/run-onboarding-funnel.sh --workdir $(CURDIR)

onboarding-sqlite: ## SQLite-only onboarding smoke test (no Docker)
	@echo "Testing SQLite-only onboarding..."
	@cd apps/zerg/backend && \
	uv run --extra dev pytest tests_lite/test_onboarding_sqlite.py -v --tb=short -p no:warnings
	@echo "SQLite onboarding smoke test passed"

qa-oss: ## Full OSS QA (isolated clone + UI gate)
	@./scripts/qa-oss.sh $(ARGS)

# ---------------------------------------------------------------------------
# Vibetest (LLM-powered browser QA — advisory only, never blocks CI)
# ---------------------------------------------------------------------------
VIBETEST_AGENTS ?= 3

vibetest: ## Run vibetest against isolated server (advisory, needs GOOGLE_API_KEY)
	@./scripts/run-vibetest.sh --agents $(VIBETEST_AGENTS)

vibetest-local: ## Run vibetest against running dev server (make dev)
	@./scripts/run-vibetest.sh --use-running http://localhost:47200 --agents $(VIBETEST_AGENTS)
