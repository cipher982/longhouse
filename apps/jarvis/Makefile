ROOT_DIR := $(abspath ../..)

.PHONY: help dev zerg test test-e2e test-chat-e2e native

help: ## Show available commands
	@echo "Jarvis (Unified SPA)" && echo "" && \
	grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

dev: ## Start unified stack (recommended)
	@$(MAKE) -C $(ROOT_DIR) dev

zerg: ## Start direct ports stack (backend + frontend)
	@$(MAKE) -C $(ROOT_DIR) zerg

test: ## Run unit tests (backend + frontend; no Playwright)
	@$(MAKE) -C $(ROOT_DIR) test

test-e2e: ## Run Playwright E2E (unified SPA)
	@$(MAKE) -C $(ROOT_DIR) test-e2e

test-chat-e2e: ## Run chat (/chat) E2E smoke tests
	@$(MAKE) -C $(ROOT_DIR) test-chat-e2e

native: ## Launch Electron shell (legacy; may be stale)
	@echo "⚠️  Jarvis is now served inside the unified SPA; native shell may be stale."
	@cd apps/native && bun run dev
