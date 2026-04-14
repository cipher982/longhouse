# Longhouse

-include .env
export $(shell sed 's/=.*//' .env 2>/dev/null || true)

COMPOSE_DEV := docker compose --project-name zerg --env-file .env -f docker/docker-compose.dev.yml

E2E_BACKEND_PORT ?=
E2E_FRONTEND_PORT ?=

.PHONY: help dev dev-demo stop test test-frontend test-engine test-runner test-control-plane test-e2e test-e2e-core test-e2e-a11y test-e2e-cp test-e2e-single test-ci test-full install-engine validate validate-ws validate-sse validate-sdk validate-makefile regen-ws regen-sse generate-sdk qa-live qa-live-chat reprovision deploy-status ui-capture test-shipper-e2e test-shipper-premerge test-wheel-package test-install test-install-first-run test-install-macos-ambient test-install-runner test-hosted-instance test-coolify-deploy test-web-entrypoint test-runtime-packaging-macos test-e2e-onboarding test-e2e-continuation-provider test-readmes test-codex-bridge-e2e test-hooks onboarding-funnel launch-gate-local lint-test-patterns import-smoke ensure-js-deps ensure-playwright-browser demo-db menubar-harness qa-oss vibetest eval

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
help: ## Show this help message
	@echo "\nLonghouse"
	@echo "========="
	@echo ""
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' Makefile | grep -v '## @internal' | sed 's/:.*## /: /' | column -t -s ':' | awk '{printf "  %-28s %s\n", $$1":", substr($$0, index($$0,$$2))}' | sort
	@echo ""

# ---------------------------------------------------------------------------
# Development
# ---------------------------------------------------------------------------
dev: ## Start dev environment (SQLite, no Docker)
	@env -u DATABASE_URL ./scripts/dev.sh

dev-demo: ## Start demo environment (seeded SQLite DB)
	@env -u DATABASE_URL ./scripts/dev-demo.sh

demo-db: ## Build demo SQLite database
	@uv run python server/scripts/build_demo_db.py

stop: ## Stop dev services
	@pkill -f "uvicorn zerg.main:app" 2>/dev/null || true
	@pkill -f "vite" 2>/dev/null || true
	@echo "Stopped"

# ---------------------------------------------------------------------------
# Testing — run the tier that matches your change
#
#  make test              backend (server/)          ~10s
#  make test-frontend     frontend (web/)            ~15s
#  make test-engine       engine (engine/)           ~20s
#  make test-runner       runner (runner/)           ~5s
#  make test-control-plane                           ~10s
#  make test-e2e          browser E2E                ~2min
#  make test-ci           pre-push                   ~3min
#  make test-full         everything                 ~8min
# ---------------------------------------------------------------------------
test: ## Backend unit tests (tests_lite/, ~10s)
	@cd server && ./run_backend_tests_lite.sh

test-frontend: ## Frontend unit tests + type-check (~15s)
	@cd web && bun run validate:types && bun run test -- --run

test-engine: ## Rust engine tests (~20s)
	cd engine && cargo build --profile $(or $(CARGO_PROFILE),release)
	cd engine && cargo test --profile $(or $(CARGO_PROFILE),release) --bin longhouse-engine --test golden_parser_contract --test adversarial_parser

test-runner: ## Runner unit tests (~5s)
	@cd runner && bun test

test-control-plane: ## Control-plane tests
	@cd control-plane && uv sync --extra dev --frozen >/dev/null && uv run --extra dev pytest tests -q

test-e2e: ## Launch-surface E2E (core + a11y)
	$(MAKE) test-e2e-core
	$(MAKE) test-e2e-a11y

test-e2e-core: ## @internal Core E2E — no retries
	@$(MAKE) ensure-playwright-browser
	cd e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) \
		bunx playwright test --project=core --retries=0 --workers=2

test-e2e-a11y: ## @internal Accessibility checks
	@$(MAKE) ensure-playwright-browser
	cd e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test --project=chromium tests/accessibility.spec.ts

test-e2e-cp: ## Control plane E2E (Playwright)
	@cd control-plane && uv sync --extra dev --frozen >/dev/null && \
		uv run --extra dev playwright install chromium --with-deps >/dev/null 2>&1 || true && \
		uv run --extra dev pytest e2e/ -v

test-e2e-single: ## @internal Run one E2E spec (TEST=tests/foo.spec.ts)
	@$(MAKE) ensure-playwright-browser
	@test -n "$(TEST)" || (echo "Usage: make test-e2e-single TEST=<spec>" && exit 1)
	cd e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test $(TEST)

test-e2e-continuation-provider: ## @internal Provider-backed continuation smoke
	@$(MAKE) ensure-js-deps
	cd web && bun run build
	cd e2e && E2E_BACKEND_PORT=$(E2E_BACKEND_PORT) node scripts/provider-continuation-smoke.mjs

test-e2e-onboarding: ## @internal Onboarding browser ring
	@ONBOARDING_PLAYWRIGHT_PROJECT="$(PROJECT)" ./scripts/qa/qa-oss.sh --workdir $(CURDIR) --no-unit --no-e2e

test-shipper-e2e: ## Shipper pipeline E2E (engine → API → DB)
	cd engine && cargo build --profile $(or $(CARGO_PROFILE),release)
	cd server && uv run --extra dev pytest tests/integration/test_shipper_e2e.py -m integration -v

test-shipper-premerge: ## Engine + shipper E2E (run before merging engine changes)
	$(MAKE) test-engine
	$(MAKE) test-shipper-e2e

test-codex-bridge-e2e: ## Codex bridge E2E
	@bash scripts/qa/test-codex-bridge-e2e.sh

test-hooks: ## Hook outbox pipeline E2E (requires daemon running)
	@./scripts/qa/test-hooks-e2e.sh

test-ci: ## Pre-push CI check (~3min)
	$(MAKE) validate
	$(MAKE) import-smoke
	$(MAKE) test-coolify-deploy
	$(MAKE) test-web-entrypoint
	$(MAKE) test
	$(MAKE) test-control-plane
	$(MAKE) test-frontend
	$(MAKE) test-runner
	$(MAKE) test-engine
	$(MAKE) test-wheel-package
	$(MAKE) test-shipper-e2e

test-full: ## Full suite — all tiers (~8min)
	$(MAKE) test
	$(MAKE) test-control-plane
	$(MAKE) test-frontend
	$(MAKE) test-runner
	$(MAKE) test-engine
	$(MAKE) test-shipper-e2e
	$(MAKE) test-e2e

# CI-referenced test helpers (keep for workflow compatibility)
test-install: ## Installer syntax + first-run smoke
	@bash -n scripts/install.sh
	@$(MAKE) test-install-first-run

test-install-first-run: ## @internal Disposable first-run installer smoke
	@./scripts/ci/installer-first-run.sh

test-install-macos-ambient: ## @internal Disposable macOS first-run smoke with menu bar install
	@./scripts/ci/installer-first-run.sh --menubar

test-install-runner: ## @internal Install-runner script tests
	@bash scripts/tests/install-runner.test.sh

test-hosted-instance: ## @internal Hosted-instance helper tests
	@bash scripts/tests/hosted-instance-auth.test.sh
	@bash scripts/tests/hosted-loop-debug.test.sh

test-coolify-deploy: ## @internal Coolify deploy helper tests
	@bash scripts/tests/coolify-deploy.test.sh

test-web-entrypoint: ## @internal Web runtime entrypoint tests
	@bash scripts/tests/web-docker-entrypoint.test.sh

test-wheel-package: ## @internal CLI wheel packaging smoke
	@./scripts/qa/test-wheel-package.sh

test-runtime-packaging-macos: ## @internal Build/sign/zip Longhouse.app locally
	@./scripts/qa/test-local-runtime-packaging.sh

test-readmes: ## @internal README contract tests (MODE=smoke|full)
	@python3 scripts/qa/run-readme-tests.py --mode $(or $(MODE),smoke) $(FILES)

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
install-engine: ## Build + install Rust engine binary
	cd engine && cargo build --release
	codesign -s - engine/target/release/longhouse-engine
	@mkdir -p $$HOME/.local/bin
	@ln -sf "$(CURDIR)/engine/target/release/longhouse-engine" "$$HOME/.local/bin/longhouse-engine"
	@echo "longhouse-engine installed"

# ---------------------------------------------------------------------------
# Validation (contract drift checks)
# ---------------------------------------------------------------------------
validate: ## Run all contract checks
	@$(MAKE) validate-ws
	@$(MAKE) validate-sse
	@$(MAKE) validate-sdk
	@$(MAKE) validate-makefile
	@$(MAKE) lint-test-patterns

validate-ws: ## @internal WebSocket contract check
	@cd server && \
		export XDG_CACHE_HOME="$$PWD/.uv_cache" TMPDIR="$$PWD/.uv_tmp"; \
		mkdir -p "$$XDG_CACHE_HOME" "$$TMPDIR"; \
		uv run --no-project --with pyyaml python ../scripts/generate/generate-ws-types-modern.py schemas/ws-protocol-asyncapi.yml >/dev/null 2>&1
	@if ! git diff --quiet server/zerg/generated/ws_messages.py web/src/generated/ws-messages.ts schemas/ws-protocol.schema.json schemas/ws-protocol-v1.json; then \
		echo "WebSocket code out of sync — run 'make regen-ws'"; \
		exit 1; \
	fi

validate-sse: ## @internal SSE contract check
	@cd server && \
		export XDG_CACHE_HOME="$$PWD/.uv_cache" TMPDIR="$$PWD/.uv_tmp"; \
		mkdir -p "$$XDG_CACHE_HOME" "$$TMPDIR"; \
		uv run --no-project --with pyyaml python ../scripts/generate/generate-sse-types.py schemas/sse-events.asyncapi.yml >/dev/null 2>&1
	@if ! git diff --quiet server/zerg/generated/sse_events.py web/src/generated/sse-events.ts schemas/sse-events.asyncapi.yml; then \
		echo "SSE code out of sync — run 'make regen-sse'"; \
		exit 1; \
	fi

validate-sdk: ## @internal OpenAPI/SDK drift check
	@$(MAKE) generate-sdk >/dev/null
	@if ! git diff --quiet -- openapi.json web/src/generated/openapi-types.ts; then \
		echo "OpenAPI/SDK out of sync — run 'make generate-sdk'"; \
		exit 1; \
	fi

validate-makefile: ## @internal Verify .PHONY vs documented targets
	@failed=0; \
	for t in $$(grep -E '^\.PHONY:' Makefile | sed -E 's/^\.PHONY:[[:space:]]*//; s/\\//g' | tr ' ' '\n' | sed '/^$$/d'); do \
		case $$t in help|validate-makefile) continue ;; esac; \
		if ! grep -Eq "^$$t:.*##" Makefile; then echo "Missing ## for .PHONY: $$t"; failed=1; fi; \
	done; \
	for t in $$(grep -E '^[a-zA-Z0-9_-]+:.*##' Makefile | sed -E 's/:.*##.*$$//'); do \
		if ! grep -Eq "^\.PHONY:.*\\b$$t\\b" Makefile; then echo "Not in .PHONY: $$t"; failed=1; fi; \
	done; \
	exit $$failed

lint-test-patterns: ## @internal Check for test anti-patterns
	@bash scripts/qa/lint-test-patterns.sh

regen-ws: ## Regenerate WebSocket contract code
	@cd server && \
		export XDG_CACHE_HOME="$$PWD/.uv_cache" TMPDIR="$$PWD/.uv_tmp"; \
		mkdir -p "$$XDG_CACHE_HOME" "$$TMPDIR"; \
		uv run --no-project --with pyyaml python ../scripts/generate/generate-ws-types-modern.py schemas/ws-protocol-asyncapi.yml

regen-sse: ## Regenerate SSE contract code
	@cd server && \
		export XDG_CACHE_HOME="$$PWD/.uv_cache" TMPDIR="$$PWD/.uv_tmp"; \
		mkdir -p "$$XDG_CACHE_HOME" "$$TMPDIR"; \
		uv run --no-project --with pyyaml python ../scripts/generate/generate-sse-types.py schemas/sse-events.asyncapi.yml

generate-sdk: ## Regenerate OpenAPI types
	@$(MAKE) ensure-js-deps
	@cd server && uv run python scripts/export_openapi.py >/dev/null
	@cd web && bun run openapi-typescript ../openapi.json --output src/generated/openapi-types.ts

import-smoke: ## @internal Fast import + CSS reference smoke (<5s)
	@cd server && uv run python ../scripts/ci/import-smoke.py

# ---------------------------------------------------------------------------
# Production / QA
# ---------------------------------------------------------------------------
qa-live: ## Post-deploy QA against hosted instance (~60s)
	@$(MAKE) ensure-js-deps
	@./scripts/qa/qa-live.sh

qa-live-chat: ## Hosted live chat smoke
	@$(MAKE) ensure-js-deps
	@./scripts/qa/run-prod-e2e.sh tests/live/live_chat_smoke.spec.ts --timeout=120000 --reporter=line

reprovision: ## Reprovision hosted instance (SUBDOMAIN=david010, optional IMAGE=...)
	@bash -c 'source scripts/lib/hosted-instance.sh && \
		lh_hosted_prepare_control_plane_auth && \
		lh_hosted_resolve_instance "$(or $(SUBDOMAIN),david010)" && \
		lh_hosted_reprovision "$$LH_INSTANCE_ID" "$(IMAGE)" && \
		echo "Reprovisioned $$LH_INSTANCE_SUBDOMAIN — waiting for health..." && \
		./scripts/ci/wait-for-http.sh "https://$$LH_INSTANCE_SUBDOMAIN.longhouse.ai/api/health" "$$LH_INSTANCE_SUBDOMAIN health" 30 2 && \
		curl -sf "https://$$LH_INSTANCE_SUBDOMAIN.longhouse.ai/api/health" | \
			python3 -c "import sys,json; print(json.load(sys.stdin)[\"status\"])"'

deploy-status: ## Show deployed SHA + health for all surfaces
	@./scripts/ops/deploy-status.sh

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
ui-capture: ## Capture local dev UI debug bundle
	@bunx tsx scripts/ui-capture.ts $(PAGE) $(if $(SCENE),--scene=$(SCENE),) $(if $(OUTPUT),--output=$(OUTPUT),) $(if $(ALL),--all,) $(if $(NO_TRACE),--no-trace,)

menubar-harness: ## macOS menu bar harness (MODE=test|fixtures|live|smoke|full|window|menubar)
	@./scripts/qa/menubar-harness.sh $(or $(MODE),test)

qa-oss: ## Full OSS QA (isolated clone + onboarding)
	@./scripts/qa/qa-oss.sh $(ARGS)

onboarding-funnel: ## @internal Onboarding funnel from README contract
	@./scripts/ops/run-onboarding-funnel.sh

launch-gate-local: test-install onboarding-funnel ## @internal Local launch gate

vibetest: ## LLM-powered browser QA (advisory, needs GOOGLE_API_KEY)
	@./scripts/qa/run-vibetest.sh --agents $(or $(AGENTS),3)

eval: ## AI evals (REAL LLM calls, costs $$$)
	@cd server && env EVAL_MODE=live uv run --extra dev pytest evals/ -v --variant=$(or $(VARIANT),baseline) --timeout=120

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
ensure-js-deps: ## @internal Install JS deps if missing
	@if [ ! -f node_modules/@playwright/test/package.json ]; then bun install --frozen-lockfile; fi

ensure-playwright-browser: ## @internal Install Playwright Chromium if missing
	@$(MAKE) ensure-js-deps
	@cd e2e && bunx playwright install chromium >/dev/null
