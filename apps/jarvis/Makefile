# Jarvis Voice Agent — simple Makefile
# Targets: help, install, start, native, test, status, stop, clean

.PHONY: help install start native test test-e2e status stop clean

help: ## Show available commands
	@echo "Jarvis Commands" && echo "" && \
	grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install all dependencies (run from repo root with bun install)
	@echo "📦 Dependencies managed by Bun at repo root"
	@echo "   Run 'bun install' from the repository root"

start: ## Start server (8787) and web app (8080)
	@echo "🚀 Starting Jarvis... (Ctrl+C to stop)"; \
	trap 'echo "\n🛑 Stopping..."; $(MAKE) stop; exit 0' INT; \
	cd apps/server && bun run dev & \
	cd apps/web && bun run dev & \
	echo "⏳ Waiting for services..."; \
	until curl -sf http://localhost:8787/session >/dev/null; do sleep 0.4; done; \
	until curl -sf http://localhost:8080 >/dev/null || curl -sf http://localhost:8081 >/dev/null; do sleep 0.4; done; \
	echo "✅ Ready: http://localhost:8080"; \
	open http://localhost:8080 2>/dev/null || open http://localhost:8081 2>/dev/null || true; \
	wait

native: ## Launch Electron shell (servers must be running)
	@cd apps/native && bun run dev

test: ## Run Playwright tests
	@bun run test

test-e2e: ## Run E2E tests (terminal output only)
	@echo "🧪 Running E2E tests (servers auto-start via Playwright)..."
	@bun run test

status: ## Check if services are up
	@echo "API (8787):"; curl -sf http://localhost:8787/session >/dev/null && echo "  ✅" || echo "  ❌"
	@echo "PWA (8080):"; curl -sf http://localhost:8080 >/dev/null && echo "  ✅" || echo "  ❌"

stop: ## Stop running dev processes (best-effort)
	@echo "🛑 Stopping Jarvis services..."
	@pkill -f "/Users/davidrose/git/zerg/apps/server" 2>/dev/null || true
	@pkill -f "/Users/davidrose/git/zerg.*vite" 2>/dev/null || true
	@pkill -f "jarvis.*electron" 2>/dev/null || true
	@pkill -f "uvx.*traccar.*jarvis" 2>/dev/null || true
	@sleep 1
	@echo "✅ Stopped"

clean: ## Remove node_modules and test artifacts
	@rm -rf node_modules apps/server/node_modules apps/web/node_modules apps/native/node_modules test-results playwright-report
	@echo "🧹 Cleaned"
