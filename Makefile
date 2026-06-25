# Longhouse

-include .env
export $(shell sed 's/=.*//' .env 2>/dev/null || true)

COMPOSE_DEV := docker compose --project-name zerg --env-file .env -f docker/docker-compose.dev.yml

E2E_BACKEND_PORT ?=
E2E_FRONTEND_PORT ?=
SOURCE_REVIEW_STATUS ?= not_run
SOURCE_REVIEW_NOTE ?= Provider release proof invoked from Makefile.
BASELINE_ROOT ?= .provider-release-proofs
CODEX_API_URL ?=
CODEX_AGENTS_TOKEN ?=
CLAUDE_API_URL ?=
CLAUDE_AGENTS_TOKEN ?=
CLAUDE_DEVICE_ID ?=

.PHONY: help dev dev-demo stop test test-ios test-ios-session-open ios-marketing test-mobile-chat test-mobile-chat-stress test-mobile-chat-replay test-ios-helper test-frontend test-engine test-runner test-e2e test-e2e-core test-e2e-a11y test-e2e-single test-ci test-full install-engine install-cli validate validate-ws validate-sdk validate-ios-api validate-makefile validate-build-identity validate-playwright-install validate-public-surface validate-managed-codex-contract validate-managed-session-contract validate-provider-cli-canaries validate-ship-monitor provider-release-proof provider-release-proof-accept provider-release-proof-diff provider-release-proof-old-new provider-release-proof-staged-old-new provider-release-proof-universal-smoke provider-release-proof-universal-live-smoke provider-release-proof-status provider-release-proof-status-all provider-release-proof-maturity regen-ws generate-sdk generate-ios-api qa-live hosted-shipper-mixed-bench qa-unmanaged render-canary session-propagation-sla managed-claude-truth-probe managed-claude-poc provider-live-route-e2e provider-live-route-e2e-opencode-transcript reprovision deploy-status launch-readiness ship-watch ship release ui-capture marketing-screenshots demo-render qa-ui-workbench qa-ui-baseline qa-ui-baseline-update qa-ui-baseline-mobile qa-visual-compare test-shipper-e2e test-shipper-synthetic-bench test-shipper-synthetic-live-bench test-shipper-premerge test-wheel-package test-install test-install-first-run test-install-macos-ambient test-install-runner test-hosted-instance test-coolify-deploy test-web-entrypoint test-runtime-packaging-macos test-e2e-onboarding test-readmes test-codex-bridge-e2e test-hooks onboarding-funnel launch-gate-local lint-test-patterns import-smoke ensure-js-deps ensure-playwright-browser demo-db menubar-harness qa-oss vibetest eval dogfood dogfood-refresh dogfood-check observability-up observability-down

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

observability-up: ## Start Prometheus+Grafana god-view stack (needs LONGHOUSE_METRICS_TOKEN)
	@docker compose -f docker/observability/docker-compose.observability.yml up -d
	@echo "Grafana: http://localhost:$${GRAFANA_PORT:-3001}  (admin / $${GRAFANA_ADMIN_PASSWORD:-admin})"

observability-down: ## Stop the god-view observability stack
	@docker compose -f docker/observability/docker-compose.observability.yml down

# ---------------------------------------------------------------------------
# Testing — run the tier that matches your change
#
#  make test              backend (server/)          ~10s
#  make test-ios          iOS (ios/)                 ~1m
#  make test-ios-session-open iOS tap-to-paint benchmark
#  make test-mobile-chat  mobile chat focused path
#  make test-ios-helper   iOS helper scripts         ~1s
#  make test-frontend     frontend (web/)            ~15s
#  make test-engine       engine (engine/)           ~20s
#  make test-runner       runner (runner/)           ~5s
#  make test-e2e          browser E2E                ~2min
#  make test-ci           pre-push                   ~3min
#  make test-full         everything                 ~8min
# ---------------------------------------------------------------------------
test: ## Backend unit tests (tests_lite/, ~10s)
	@cd server && ./run_backend_tests_lite.sh

test-ios: ## iOS unit + smoke tests (simulator)
	@python3 scripts/build/generate_build_identity.py
	@bash scripts/build/stage_ios_build_identity.sh
	@xcodegen --spec ios/XcodeHarness/project.yml --project-root ios/XcodeHarness
	@DESTINATION="$$(python3 scripts/ci/select_ios_simulator.py ios/XcodeHarness/LonghouseIOS.xcodeproj Longhouse)"; \
	IOS_TEST_SCHEMES="Longhouse LonghouseSmoke" ./scripts/ci/run_ios_tests.sh "$$DESTINATION"

test-ios-session-open: ## iOS simulator timeline tap-to-transcript benchmark
	@python3 scripts/build/generate_build_identity.py
	@bash scripts/build/stage_ios_build_identity.sh
	@xcodegen --spec ios/XcodeHarness/project.yml --project-root ios/XcodeHarness
	@DESTINATION="$$(python3 scripts/ci/select_ios_simulator.py ios/XcodeHarness/LonghouseIOS.xcodeproj LonghouseChatStress)"; \
	DERIVED_DATA_PATH="$${IOS_DERIVED_DATA_PATH:-$$HOME/Library/Developer/Xcode/DerivedData/LonghouseIOS-SessionOpen}"; \
	mkdir -p "$$DERIVED_DATA_PATH"; \
	LONGHOUSE_UI_TEST_MOBILE_TAIL_DELAY_MS="$${IOS_SESSION_OPEN_DELAY_MS:-0}" \
	xcodebuild \
		-project ios/XcodeHarness/LonghouseIOS.xcodeproj \
		-scheme LonghouseChatStress \
		-destination "$$DESTINATION" \
		-derivedDataPath "$$DERIVED_DATA_PATH" \
		-only-testing:LonghouseChatStressUITests/SessionOpenPerformanceUITests/testTimelineTapToTranscriptPaintPerformance \
		test

ios-marketing: ## Capture iOS marketing screenshots to /tmp/lh-shots/ (session-light.png, session-dark.png)
	@python3 scripts/build/generate_build_identity.py
	@bash scripts/build/stage_ios_build_identity.sh
	@xcodegen --spec ios/XcodeHarness/project.yml --project-root ios/XcodeHarness
	@DESTINATION="$$(python3 scripts/ci/select_ios_simulator.py ios/XcodeHarness/LonghouseIOS.xcodeproj LonghouseMarketingCaptures)"; \
	rm -rf /tmp/lh-shots; \
	UDID="$$(printf '%s' "$$DESTINATION" | sed -n 's/.*id=\([0-9A-Fa-f-]*\).*/\1/p')"; \
	if [ -n "$$UDID" ]; then \
		xcrun simctl boot "$$UDID" 2>/dev/null || true; \
		xcrun simctl bootstatus "$$UDID" -b 2>/dev/null || true; \
		xcrun simctl status_bar "$$UDID" override --time "9:41" --batteryState charged --batteryLevel 100 --wifiBars 3 --cellularBars 4 2>/dev/null || true; \
	fi; \
	xcodebuild \
		-project ios/XcodeHarness/LonghouseIOS.xcodeproj \
		-scheme LonghouseMarketingCaptures \
		-destination "$$DESTINATION" \
		-derivedDataPath "$$(mktemp -d)" \
		-testPlan MarketingCaptures \
		test; \
	[ -n "$$UDID" ] && xcrun simctl status_bar "$$UDID" clear 2>/dev/null || true; \
	echo "Captures written to /tmp/lh-shots/"; \
	ls -lh /tmp/lh-shots/

test-mobile-chat: ## Focused mobile chat validation (web telemetry + iOS unit tests)
	@cd web && bun run test -- --run src/components/session-workspace/__tests__/RenderTelemetryPanel.test.tsx src/pages/__tests__/SessionDetailPage.test.tsx
	@python3 scripts/build/generate_build_identity.py
	@bash scripts/build/stage_ios_build_identity.sh
	@xcodegen --spec ios/XcodeHarness/project.yml --project-root ios/XcodeHarness
	@DESTINATION="$$(python3 scripts/ci/select_ios_simulator.py ios/XcodeHarness/LonghouseIOS.xcodeproj Longhouse)"; \
	DERIVED_DATA_PATH="$${IOS_DERIVED_DATA_PATH:-$$HOME/Library/Developer/Xcode/DerivedData/LonghouseIOS-MobileChat}"; \
	mkdir -p "$$DERIVED_DATA_PATH"; \
	xcodebuild \
		-project ios/XcodeHarness/LonghouseIOS.xcodeproj \
		-scheme Longhouse \
		-destination "$$DESTINATION" \
		-derivedDataPath "$$DERIVED_DATA_PATH" \
		test

test-mobile-chat-stress: ## Holistic iOS mobile chat fixture stress test
	@python3 scripts/build/generate_build_identity.py
	@bash scripts/build/stage_ios_build_identity.sh
	@xcodegen --spec ios/XcodeHarness/project.yml --project-root ios/XcodeHarness
	@rm -f /tmp/longhouse-chat-replay.json
	@DESTINATION="$$(python3 scripts/ci/select_ios_simulator.py ios/XcodeHarness/LonghouseIOS.xcodeproj LonghouseChatStress)"; \
	IOS_TEST_SCHEMES="LonghouseChatStress" ./scripts/ci/run_ios_tests.sh "$$DESTINATION"

test-mobile-chat-replay: ## Replay a local SQLite transcript through the iOS mobile chat stress test
	@python3 scripts/build/generate_build_identity.py
	@bash scripts/build/stage_ios_build_identity.sh
	@xcodegen --spec ios/XcodeHarness/project.yml --project-root ios/XcodeHarness
	@REPLAY_PATH="/tmp/longhouse-chat-replay.json"; \
	trap 'rm -f "$$REPLAY_PATH"' EXIT; \
	SESSION_ARGS=""; \
	if [ -n "$${MOBILE_CHAT_REPLAY_SESSION:-}" ]; then SESSION_ARGS="--session-id $$MOBILE_CHAT_REPLAY_SESSION"; fi; \
	python3 scripts/qa/export-ios-chat-replay.py \
		--db "$${MOBILE_CHAT_REPLAY_DB:-$$HOME/.longhouse/longhouse.db}" \
		$$SESSION_ARGS \
		--limit "$${MOBILE_CHAT_REPLAY_LIMIT:-800}" \
		--output "$$REPLAY_PATH"; \
	DESTINATION="$$(python3 scripts/ci/select_ios_simulator.py ios/XcodeHarness/LonghouseIOS.xcodeproj LonghouseChatStress)"; \
	LONGHOUSE_UI_TEST_CHAT_REPLAY_PATH="$$REPLAY_PATH" \
	IOS_TEST_SCHEMES="LonghouseChatStress" ./scripts/ci/run_ios_tests.sh "$$DESTINATION"

test-ios-helper: ## iOS simulator helper script tests
	@bash scripts/tests/select-ios-simulator.test.sh

test-frontend: ## Frontend unit tests + type-check (~15s)
	@cd web && bun run validate:types && bun run test -- --run

test-engine: ## Rust engine tests (~20s)
	@python3 scripts/build/generate_build_identity.py
	cd engine && cargo build --profile $(or $(CARGO_PROFILE),release)
	cd engine && cargo test --profile $(or $(CARGO_PROFILE),release) --bin longhouse-engine --test golden_parser_contract --test adversarial_parser

test-runner: ## Runner unit tests (~5s)
	@cd runner && bun test

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

test-e2e-single: ## @internal Run one E2E spec (TEST=tests/foo.spec.ts)
	@$(MAKE) ensure-playwright-browser
	@test -n "$(TEST)" || (echo "Usage: make test-e2e-single TEST=<spec>" && exit 1)
	cd e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) bunx playwright test $(TEST)

test-e2e-onboarding: ## @internal Onboarding browser ring
	@ONBOARDING_PLAYWRIGHT_PROJECT="$(PROJECT)" ./scripts/qa/qa-oss.sh --workdir $(CURDIR) --no-unit --no-e2e

test-shipper-e2e: ## Shipper pipeline E2E (engine → API → DB)
	cd engine && cargo build --profile $(or $(CARGO_PROFILE),release)
	cd server && uv run --extra dev pytest tests/integration/test_shipper_e2e.py -m integration -v

test-shipper-synthetic-bench: ## Synthetic shipper bench fixture gate
	@python3 scripts/build/generate_build_identity.py
	cd engine && cargo run --profile $(or $(CARGO_PROFILE),release) -- bench --synthetic-files 8 --synthetic-events-per-file 100 --synthetic-bytes-per-event 1024 --level L3 --compress --parallel --workers 2

test-shipper-synthetic-live-bench: ## Synthetic mixed live/archive shipper bench
	@python3 scripts/build/generate_build_identity.py
	@port_file="$$(mktemp)"; \
	python3 scripts/qa/shipper_synthetic_echo.py --port-file "$$port_file" & server_pid="$$!"; \
	cleanup() { kill "$$server_pid" >/dev/null 2>&1 || true; rm -f "$$port_file"; }; \
	trap cleanup EXIT INT TERM; \
	for _ in 1 2 3 4 5 6 7 8 9 10; do test -s "$$port_file" && break; sleep 0.2; done; \
	test -s "$$port_file"; \
	port="$$(cat "$$port_file")"; \
	cd engine && cargo run --profile $(or $(CARGO_PROFILE),release) -- bench --synthetic-files 6 --synthetic-events-per-file 50 --synthetic-bytes-per-event 1024 --level L3 --ship-url "http://127.0.0.1:$$port" --ship-token synthetic --ship-concurrency 4 --mixed-live-count 8 --mixed-live-max-p95-ms 10000

test-shipper-premerge: ## Engine + shipper E2E (run before merging engine changes)
	$(MAKE) test-engine
	$(MAKE) test-shipper-e2e
	$(MAKE) test-shipper-synthetic-bench
	$(MAKE) test-shipper-synthetic-live-bench

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
	$(MAKE) test-frontend
	$(MAKE) test-runner
	$(MAKE) test-engine
	$(MAKE) test-wheel-package
	$(MAKE) test-shipper-e2e

test-full: ## Full suite — all tiers (~8min)
	$(MAKE) test
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
	@bash scripts/tests/hosted-session-debug.test.sh

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
	@python3 scripts/build/generate_build_identity.py
	cd engine && cargo build --release
	codesign -s - engine/target/release/longhouse-engine
	@mkdir -p $$HOME/.local/bin
	@rm -f "$$HOME/.local/bin/longhouse-engine"
	@install -m 755 "$(CURDIR)/engine/target/release/longhouse-engine" "$$HOME/.local/bin/longhouse-engine"
	@echo "longhouse-engine installed"

install-cli: ## Reinstall the longhouse CLI from current repo source (no engine/hooks/app refresh)
	@python3 scripts/build/generate_build_identity.py
	cd server && uv tool install -e . --reinstall
	@echo "longhouse CLI installed"

dogfood: dogfood-refresh ## Refresh the real local runtime from current repo source

dogfood-refresh: ## Rebuild/reinstall the actual local Longhouse runtime from current repo source
	@./scripts/dev/dogfood-runtime.sh refresh

dogfood-check: ## Show installed local runtime status + local health
	@./scripts/dev/dogfood-runtime.sh check

# ---------------------------------------------------------------------------
# Validation (contract drift checks)
# ---------------------------------------------------------------------------
validate: ## Run all contract checks
	@$(MAKE) validate-ws
	@$(MAKE) validate-sdk
	@$(MAKE) validate-ios-api
	@$(MAKE) validate-makefile
	@$(MAKE) validate-build-identity
	@$(MAKE) validate-playwright-install
	@$(MAKE) validate-public-surface
	@$(MAKE) validate-managed-codex-contract
	@$(MAKE) validate-managed-session-contract
	@$(MAKE) validate-provider-cli-canaries
	@$(MAKE) validate-ship-monitor
	@$(MAKE) lint-test-patterns

validate-playwright-install: ## @internal Playwright installer wrapper regression tests
	@python3 scripts/tests/playwright-install.test.py

validate-public-surface: ## @internal Guard public docs against private/local leakage
	@python3 scripts/tests/public-surface-scan.test.py

validate-ship-monitor: ## @internal Ship monitor regression tests
	@python3 scripts/tests/ship-monitor.test.py

validate-build-identity: ## @internal Build identity freshness check
	@python3 scripts/build/generate_build_identity.py >/dev/null
	@python3 scripts/build/check_build_identity_fresh.py

validate-managed-codex-contract: ## @internal Guard against reintroducing packaged managed Codex runtimes
	@bash scripts/qa/check-managed-codex-contract.sh
	@python3 scripts/tests/managed-codex-contract.test.py

validate-managed-session-contract: ## @internal Guard managed provider session control/state contracts
	@bash scripts/qa/check-managed-session-contract.sh
	@python3 scripts/tests/managed-session-contract.test.py

validate-provider-cli-canaries: ## @internal Provider release canary wrapper tests
	@python3 scripts/tests/codex-provider-release-canary.test.py
	@python3 scripts/tests/provider-release-profile-canary.test.py
	@python3 scripts/tests/provider-release-proof-coverage.test.py
	@python3 scripts/tests/provider-release-proof.test.py
	@python3 scripts/tests/provider-release-proof-baseline.test.py
	@python3 scripts/tests/provider-release-proof-old-new.test.py
	@python3 scripts/tests/provider-release-proof-maturity.test.py
	@python3 scripts/tests/provider-release-proof-make.test.py
	@python3 scripts/tests/provider-control-e2e-canary.test.py
	@python3 scripts/tests/provider-live-canary.test.py
	@python3 scripts/tests/provider-live-proof-publish.test.py
	@python3 scripts/tests/provider-live-route-e2e.test.py
	@$(MAKE) provider-release-proof-universal-smoke

provider-release-proof: ## Emit provider release proof artifact; set PROVIDER=... and optional PROVIDER_BIN=...
	@set -eu; \
	if [ -z "$(PROVIDER)" ]; then echo "PROVIDER is required" >&2; exit 2; fi; \
	set -- scripts/qa/provider-release-proof.py \
		--provider "$(PROVIDER)" \
		--source-review-status "$(SOURCE_REVIEW_STATUS)" \
		--source-review-note "$(SOURCE_REVIEW_NOTE)" \
		--json; \
	if [ -n "$(PROVIDER_BIN)" ]; then set -- "$$@" --provider-bin "$(PROVIDER_BIN)"; fi; \
	if [ -n "$(PROVIDER_VERSION)" ]; then set -- "$$@" --provider-version "$(PROVIDER_VERSION)"; fi; \
	if [ -n "$(ARTIFACT)" ]; then set -- "$$@" --artifact "$(ARTIFACT)"; fi; \
	if [ -n "$(EVIDENCE_ROOT)" ]; then set -- "$$@" --evidence-root "$(EVIDENCE_ROOT)"; fi; \
	if [ -n "$(SCENARIO_ID)" ]; then set -- "$$@" --scenario-id "$(SCENARIO_ID)"; fi; \
	if [ -n "$(PREFLIGHT_ONLY)" ]; then set -- "$$@" --preflight-only; fi; \
	if [ -n "$(TIMEOUT_SECS)" ]; then set -- "$$@" --timeout-secs "$(TIMEOUT_SECS)"; fi; \
	if [ -n "$(CODEX_RUN_FAKE_APP_SERVER)" ]; then set -- "$$@" --codex-run-fake-app-server; fi; \
	if [ -n "$(CODEX_RUN_RAW_FRESH_REMOTE)" ]; then set -- "$$@" --codex-run-raw-fresh-remote; fi; \
	if [ -n "$(CODEX_RUN_MANAGED_TUI_ATTACH)" ]; then set -- "$$@" --codex-run-managed-tui-attach; fi; \
	if [ -n "$(CODEX_RUN_DETACHED_UI)" ]; then set -- "$$@" --codex-run-detached-ui; fi; \
	if [ -n "$(CODEX_RUN_MANAGED_LIVE_SEND)" ]; then set -- "$$@" --codex-run-managed-live-send; fi; \
	if [ -n "$(CODEX_RUN_MANAGED_LIVE_INTERRUPT)" ]; then set -- "$$@" --codex-run-managed-live-interrupt; fi; \
	if [ -n "$(CODEX_LIVE_INTERRUPT_TIMEOUT_SECS)" ]; then set -- "$$@" --codex-live-interrupt-timeout-secs "$(CODEX_LIVE_INTERRUPT_TIMEOUT_SECS)"; fi; \
	if [ -n "$(CODEX_RUN_REAL_TOOL)" ]; then set -- "$$@" --codex-run-real-tool; fi; \
	if [ -n "$(CODEX_REAL_TOOL_TIMEOUT_SECS)" ]; then set -- "$$@" --codex-real-tool-timeout-secs "$(CODEX_REAL_TOOL_TIMEOUT_SECS)"; fi; \
	if [ -n "$(CODEX_API_URL)" ]; then set -- "$$@" --codex-api-url "$(CODEX_API_URL)"; fi; \
	if [ -n "$(CLAUDE_RUN_MACHINE_LIVE_PROOF)" ]; then set -- "$$@" --claude-run-machine-live-proof; fi; \
	if [ -n "$(CLAUDE_RUN_REAL_PRINT)" ]; then set -- "$$@" --claude-run-real-print; fi; \
	if [ -n "$(CLAUDE_API_URL)" ]; then set -- "$$@" --claude-api-url "$(CLAUDE_API_URL)"; fi; \
	if [ -n "$(CLAUDE_DEVICE_ID)" ]; then set -- "$$@" --claude-device-id "$(CLAUDE_DEVICE_ID)"; fi; \
	if [ -n "$(CLAUDE_PRINT_TIMEOUT_SECS)" ]; then set -- "$$@" --claude-print-timeout-secs "$(CLAUDE_PRINT_TIMEOUT_SECS)"; fi; \
	if [ -n "$(OPENCODE_RUN_REAL_TOOL)" ]; then set -- "$$@" --opencode-run-real-tool; fi; \
	if [ -n "$(OPENCODE_RUN_TIMEOUT_SECS)" ]; then set -- "$$@" --opencode-run-timeout-secs "$(OPENCODE_RUN_TIMEOUT_SECS)"; fi; \
	if [ -n "$(ANTIGRAVITY_RUN_REAL_AGY_SEND)" ]; then set -- "$$@" --antigravity-run-real-agy-send; fi; \
	python3 "$$@"

provider-release-proof-accept: ## Accept provider proof baseline; set PROOF=... and optional BASELINE_ROOT=...
	@set -eu; \
	if [ -z "$(PROOF)" ]; then echo "PROOF is required" >&2; exit 2; fi; \
	set -- scripts/qa/provider-release-proof-baseline.py accept \
		--proof "$(PROOF)" \
		--baseline-root "$(BASELINE_ROOT)" \
		--json; \
	if [ -n "$(ARTIFACT)" ]; then set -- "$$@" --artifact "$(ARTIFACT)"; fi; \
	python3 "$$@"

provider-release-proof-diff: ## Diff provider proof artifact; set CANDIDATE=... and optional BASELINE_ROOT=... or BASE=...
	@set -eu; \
	if [ -z "$(CANDIDATE)" ]; then echo "CANDIDATE is required" >&2; exit 2; fi; \
	set -- scripts/qa/provider-release-proof-baseline.py diff \
		--candidate "$(CANDIDATE)" \
		--baseline-root "$(BASELINE_ROOT)" \
		--json; \
	if [ -n "$(BASE)" ]; then set -- "$$@" --base "$(BASE)"; fi; \
	if [ -n "$(ARTIFACT)" ]; then set -- "$$@" --artifact "$(ARTIFACT)"; fi; \
	python3 "$$@"

provider-release-proof-old-new: ## Diff explicit old/new provider proof artifacts; set OLD=... and NEW=...
	@set -eu; \
	if [ -z "$(OLD)" ]; then echo "OLD is required" >&2; exit 2; fi; \
	if [ -z "$(NEW)" ]; then echo "NEW is required" >&2; exit 2; fi; \
	set -- scripts/qa/provider-release-proof-baseline.py old-new \
		--old "$(OLD)" \
		--new "$(NEW)" \
		--baseline-root "$(BASELINE_ROOT)" \
		--json; \
	if [ -n "$(ARTIFACT)" ]; then set -- "$$@" --artifact "$(ARTIFACT)"; fi; \
	python3 "$$@"

provider-release-proof-staged-old-new: ## Run old/new staged provider binaries then diff; set PROVIDER=..., OLD_PROVIDER_BIN=..., NEW_PROVIDER_BIN=...
	@set -eu; \
	if [ -z "$(PROVIDER)" ]; then echo "PROVIDER is required" >&2; exit 2; fi; \
	if [ -z "$(OLD_PROVIDER_BIN)" ]; then echo "OLD_PROVIDER_BIN is required" >&2; exit 2; fi; \
	if [ -z "$(NEW_PROVIDER_BIN)" ]; then echo "NEW_PROVIDER_BIN is required" >&2; exit 2; fi; \
	set -- scripts/qa/provider-release-proof-old-new.py \
		--provider "$(PROVIDER)" \
		--old-provider-bin "$(OLD_PROVIDER_BIN)" \
		--new-provider-bin "$(NEW_PROVIDER_BIN)" \
		--baseline-root "$(BASELINE_ROOT)" \
		--source-review-status "$(SOURCE_REVIEW_STATUS)" \
		--source-review-note "$(SOURCE_REVIEW_NOTE)" \
		--json; \
	if [ -n "$(OLD_PROVIDER_VERSION)" ]; then set -- "$$@" --old-provider-version "$(OLD_PROVIDER_VERSION)"; fi; \
	if [ -n "$(NEW_PROVIDER_VERSION)" ]; then set -- "$$@" --new-provider-version "$(NEW_PROVIDER_VERSION)"; fi; \
	if [ -n "$(OLD_PROVIDER_SOURCE_URI)" ]; then set -- "$$@" --old-provider-source-uri "$(OLD_PROVIDER_SOURCE_URI)"; fi; \
	if [ -n "$(NEW_PROVIDER_SOURCE_URI)" ]; then set -- "$$@" --new-provider-source-uri "$(NEW_PROVIDER_SOURCE_URI)"; fi; \
	if [ -n "$(ARTIFACT)" ]; then set -- "$$@" --artifact "$(ARTIFACT)"; fi; \
	if [ -n "$(EVIDENCE_ROOT)" ]; then set -- "$$@" --evidence-root "$(EVIDENCE_ROOT)"; fi; \
	if [ -n "$(TIMEOUT_SECS)" ]; then set -- "$$@" --timeout-secs "$(TIMEOUT_SECS)"; fi; \
	if [ -n "$(SKIP_UNIVERSAL_HARNESS)" ]; then set -- "$$@" --skip-universal-harness; fi; \
	if [ -n "$(UNIVERSAL_SCENARIO)" ]; then set -- "$$@" --universal-scenario "$(UNIVERSAL_SCENARIO)"; fi; \
	if [ -n "$(UNIVERSAL_FIXTURE_PATH)" ]; then set -- "$$@" --universal-fixture-path "$(UNIVERSAL_FIXTURE_PATH)"; fi; \
	if [ -n "$(UNIVERSAL_PROMPT)" ]; then set -- "$$@" --universal-prompt "$(UNIVERSAL_PROMPT)"; fi; \
	python3 "$$@"

provider-release-proof-universal-smoke: ## Run all-provider fake/no-token universal release-proof smoke
	@set -eu; \
	set -- scripts/qa/provider-release-proof-universal-smoke.py; \
	if [ -n "$(ARTIFACT)" ]; then set -- "$$@" --artifact "$(ARTIFACT)"; fi; \
	if [ -n "$(EVIDENCE_ROOT)" ]; then set -- "$$@" --evidence-root "$(EVIDENCE_ROOT)"; fi; \
	if [ -n "$(JSON)" ]; then set -- "$$@" --json; fi; \
	if [ -n "$(UNIVERSAL_SCENARIO)" ]; then for scenario in $(UNIVERSAL_SCENARIO); do set -- "$$@" --scenario "$$scenario"; done; fi; \
	uv run --project server python "$$@"

provider-release-proof-universal-live-smoke: ## Run all-provider real-bin universal smoke including live-token streaming
	@set -eu; \
	set -- scripts/qa/provider-release-proof-universal-smoke.py --use-real-provider-bins --include-live-token-streaming; \
	if [ -n "$(ARTIFACT)" ]; then set -- "$$@" --artifact "$(ARTIFACT)"; fi; \
	if [ -n "$(EVIDENCE_ROOT)" ]; then set -- "$$@" --evidence-root "$(EVIDENCE_ROOT)"; fi; \
	if [ -n "$(JSON)" ]; then set -- "$$@" --json; fi; \
	if [ -n "$(UNIVERSAL_SCENARIO)" ]; then for scenario in $(UNIVERSAL_SCENARIO); do set -- "$$@" --scenario "$$scenario"; done; fi; \
	uv run --project server python "$$@"

provider-release-proof-status: ## Inspect accepted proof baseline; set PROVIDER=... and SCENARIO_ID=...
	@set -eu; \
	if [ -z "$(PROVIDER)" ]; then echo "PROVIDER is required" >&2; exit 2; fi; \
	if [ -z "$(SCENARIO_ID)" ]; then echo "SCENARIO_ID is required" >&2; exit 2; fi; \
	set -- scripts/qa/provider-release-proof-baseline.py status \
		--provider "$(PROVIDER)" \
		--scenario-id "$(SCENARIO_ID)" \
		--baseline-root "$(BASELINE_ROOT)" \
		--json; \
	if [ -n "$(ARTIFACT)" ]; then set -- "$$@" --artifact "$(ARTIFACT)"; fi; \
	python3 "$$@"

provider-release-proof-status-all: ## Inspect all accepted provider proof baselines from the coverage inventory
	@set -eu; \
	set -- scripts/qa/provider-release-proof-baseline.py status-all \
		--baseline-root "$(BASELINE_ROOT)" \
		--json; \
	if [ -n "$(COVERAGE)" ]; then set -- "$$@" --coverage "$(COVERAGE)"; fi; \
	if [ -n "$(ARTIFACT)" ]; then set -- "$$@" --artifact "$(ARTIFACT)"; fi; \
	python3 "$$@"

provider-release-proof-maturity: ## Emit provider release-proof maturity rollups from coverage/baselines/universal artifacts
	@set -eu; \
	set -- scripts/qa/provider-release-proof-maturity.py --json; \
	if [ -n "$(COVERAGE)" ]; then set -- "$$@" --coverage "$(COVERAGE)"; fi; \
	if [ -n "$(BASELINE_ROOT)" ]; then set -- "$$@" --baseline-root "$(BASELINE_ROOT)"; fi; \
	if [ -n "$(UNIVERSAL_ARTIFACT)" ]; then set -- "$$@" --universal-artifact "$(UNIVERSAL_ARTIFACT)"; fi; \
	if [ -n "$(ARTIFACT)" ]; then set -- "$$@" --artifact "$(ARTIFACT)"; fi; \
	python3 "$$@"

validate-ws: ## @internal WebSocket contract check
	@cd server && \
		export XDG_CACHE_HOME="$$PWD/.uv_cache" TMPDIR="$$PWD/.uv_tmp"; \
		mkdir -p "$$XDG_CACHE_HOME" "$$TMPDIR"; \
		uv run --no-project --with pyyaml python ../scripts/generate/generate-ws-types-modern.py schemas/ws-protocol-asyncapi.yml >/dev/null 2>&1
	@if ! git diff --quiet server/zerg/generated/ws_messages.py web/src/generated/ws-messages.ts schemas/ws-protocol.schema.json schemas/ws-protocol-v1.json; then \
		echo "WebSocket code out of sync — run 'make regen-ws'"; \
		exit 1; \
	fi

validate-sdk: ## @internal OpenAPI/SDK drift check
	@$(MAKE) generate-sdk >/dev/null
	@if ! git diff --quiet -- openapi.json web/src/generated/openapi-types.ts ios/Sources/Shared/Generated/SessionAPI.generated.swift; then \
		echo "OpenAPI/SDK out of sync — run 'make generate-sdk'"; \
		exit 1; \
	fi

validate-ios-api: ## @internal iOS OpenAPI DTO drift check
	@python3 scripts/generate/ios_api_models.py --check

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

generate-sdk: ## Regenerate OpenAPI types
	@$(MAKE) ensure-js-deps
	@cd server && uv run python scripts/export_openapi.py >/dev/null
	@cd web && bun run openapi-typescript ../openapi.json --output src/generated/openapi-types.ts
	@python3 scripts/generate/ios_api_models.py

generate-ios-api: ## Regenerate iOS OpenAPI DTOs from openapi.json
	@python3 scripts/generate/ios_api_models.py

import-smoke: ## @internal Fast import + CSS reference smoke (<5s)
	@cd server && uv run python ../scripts/ci/import-smoke.py

# ---------------------------------------------------------------------------
# Production / QA
# ---------------------------------------------------------------------------
qa-live: ## Canonical post-deploy hosted QA, including continuation readiness (~60s)
	@$(MAKE) ensure-js-deps
	@./scripts/qa/qa-live.sh

hosted-shipper-mixed-bench: ## Hosted mixed live/archive ingest bench
	@./scripts/qa/hosted-shipper-mixed-bench.sh

render-canary: ## Playwright render-latency check against hosted (~2min)
	@$(MAKE) ensure-js-deps
	@./scripts/qa/render-canary.sh

session-propagation-sla: ## Managed Codex warm realtime SLA probe with contaminated-run retries
	@./scripts/ci/session-propagation-sla.sh

managed-claude-truth-probe: ## Observe local/hosted truth for one managed Claude session
	@./scripts/ops/probe-managed-claude-truth.py $(ARGS)

managed-claude-poc: ## Launch one managed Claude channel POC and capture truth artifacts
	@./scripts/ops/run-managed-claude-poc.py $(ARGS)

PROVIDER_LIVE_ROUTE_PROVIDER ?= auto
provider-live-route-e2e: ## Hosted Machine Agent provider-live route E2E (PROVIDER_LIVE_ROUTE_PROVIDER=auto|provider|all)
	@./scripts/qa/provider-live-route-e2e.py --provider "$(PROVIDER_LIVE_ROUTE_PROVIDER)" $(ARGS)

provider-live-route-e2e-opencode-transcript: ## Hosted OpenCode route E2E requiring transcript archive
	@./scripts/qa/provider-live-route-e2e.py --provider opencode --require-opencode-transcript $(ARGS)

qa-unmanaged: ## Local smoke for bare Claude/Codex compatibility ingest
	@./scripts/qa/qa-unmanaged.sh

reprovision: ## Reprovision hosted instance (SUBDOMAIN=$LONGHOUSE_DEFAULT_SUBDOMAIN, optional IMAGE=...)
	@bash -c 'source scripts/lib/hosted-instance.sh && \
		lh_hosted_prepare_control_plane_auth && \
		lh_hosted_resolve_instance "$(or $(SUBDOMAIN),$(LONGHOUSE_DEFAULT_SUBDOMAIN),demo)" && \
		lh_hosted_reprovision "$$LH_INSTANCE_ID" "$(IMAGE)" && \
		echo "Reprovisioned $$LH_INSTANCE_SUBDOMAIN — waiting for health..." && \
		./scripts/ci/wait-for-http.sh "https://$$LH_INSTANCE_SUBDOMAIN.longhouse.ai/api/health" "$$LH_INSTANCE_SUBDOMAIN health" 30 2 && \
		curl -sf "https://$$LH_INSTANCE_SUBDOMAIN.longhouse.ai/api/health" | \
			python3 -c "import sys,json; print(json.load(sys.stdin)[\"status\"])"'

deploy-status: ## Show deployed SHA + health for all surfaces
	@./scripts/ops/deploy-status.sh

launch-readiness: ## Verify launch-critical workflows, live surfaces, release, and PyPI all match SHA
	@./scripts/ops/launch-readiness.py $(if $(SHA),--sha $(SHA),) $(ARGS)

ship-watch: ## Wait for exact-SHA push workflows + live deploy verification (SHA defaults to HEAD)
	@./scripts/ops/ship-monitor.py $(if $(SHA),--sha $(SHA),) $(ARGS)

ship: ## Push current HEAD, then wait for exact-SHA push workflows + live deploy verification
	@./scripts/ops/ship.sh $(if $(SHA),--sha $(SHA),) $(ARGS)

release: ## Cut a stable release (usage: make release VERSION=v0.1.13)
	@test -n "$(VERSION)" || (echo "Usage: make release VERSION=vX.Y.Z" >&2; exit 2)
	@./scripts/ops/release.sh $(VERSION)

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
ui-capture: ## Capture local dev UI debug bundle
	@bunx tsx scripts/ui-capture.ts $(PAGE) $(if $(SCENE),--scene=$(SCENE),) $(if $(VIEWPORT),--viewport=$(VIEWPORT),) $(if $(OUTPUT),--output=$(OUTPUT),) $(if $(ALL),--all,) $(if $(NO_TRACE),--no-trace,)

marketing-screenshots: ## Regenerate landing-page marketing screenshots (retina, frozen clock, demo data). NAME=<entry> for one.
	@./scripts/marketing-screenshots.sh $(NAME)

demo-render: ## Render the wedge demo (mp4 + hero poster) from real captured shots
	@echo "Gathering captured shots into video/public/shots/ ..."
	@mkdir -p video/public/shots
	@cp web/public/images/landing/timeline-preview.png video/public/shots/timeline-preview.png
	@if [ -f /tmp/lh-shots/session-dark.png ]; then \
		cp /tmp/lh-shots/session-dark.png video/public/shots/session-dark.png; \
	else \
		echo "  WARN: /tmp/lh-shots/session-dark.png missing — run 'make ios-marketing' first for the steer shot"; \
	fi
	@cd video && bun run render:wedge && bun run render:wedge-poster
	@echo "Wedge demo: video/out/wedge-demo.mp4  +  video/out/wedge-poster.png"

qa-ui-workbench: ## Capture fixture-backed timeline/session workbench screenshots
	@set -e; \
	RUN_DIR="artifacts/ui-capture/workbench-$$(date -u +%Y%m%dT%H%M%SZ)"; \
	echo "Output: $$RUN_DIR"; \
	$(MAKE) ui-capture PAGE=timeline SCENE=timeline-card-stress VIEWPORT=desktop NO_TRACE=1 OUTPUT=$$RUN_DIR/timeline-desktop; \
	$(MAKE) ui-capture PAGE=timeline SCENE=timeline-card-stress VIEWPORT=mobile NO_TRACE=1 OUTPUT=$$RUN_DIR/timeline-mobile; \
	$(MAKE) ui-capture PAGE=session-detail SCENE=session-detail-stress VIEWPORT=desktop NO_TRACE=1 OUTPUT=$$RUN_DIR/session-detail-desktop; \
	$(MAKE) ui-capture PAGE=session-detail SCENE=session-detail-stress VIEWPORT=mobile NO_TRACE=1 OUTPUT=$$RUN_DIR/session-detail-mobile; \
	bunx tsx scripts/ui-workbench-report.ts $$RUN_DIR; \
	echo "Workbench bundle: $$RUN_DIR"

qa-ui-baseline: ## Visual baseline check for current app and public pages
	@$(MAKE) ensure-playwright-browser
	cd e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) \
		bunx playwright test --project=chromium tests/ui_baseline_app.spec.ts tests/ui_baseline_public.spec.ts --workers=1

qa-ui-baseline-update: ## Update visual baselines for current app, public, and mobile pages
	@$(MAKE) ensure-playwright-browser
	cd e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) \
		bunx playwright test --project=chromium tests/ui_baseline_app.spec.ts tests/ui_baseline_public.spec.ts --update-snapshots --workers=1
	cd e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) \
		bunx playwright test --project=chromium tests/mobile/ui_baseline_mobile.spec.ts --update-snapshots --workers=1

qa-ui-baseline-mobile: ## Visual baseline check for mobile app pages
	@$(MAKE) ensure-playwright-browser
	cd e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) \
		bunx playwright test --project=chromium tests/mobile/ui_baseline_mobile.spec.ts --workers=1

qa-visual-compare: ## Compare current app screenshots against baselines; set SKIP_LLM=1 to skip LLM triage
	@$(MAKE) ensure-playwright-browser
	cd e2e && BACKEND_PORT=$(E2E_BACKEND_PORT) FRONTEND_PORT=$(E2E_FRONTEND_PORT) \
		bunx playwright test --project=chromium tests/visual_compare.spec.ts --workers=1

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
