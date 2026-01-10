/**
 * Readiness Contract for Zerg Frontend Pages
 *
 * This module defines the readiness signaling contract used across all pages
 * for E2E tests and marketing screenshot automation.
 *
 * == CONTRACT ==
 *
 * 1. data-ready="true" on document.body
 *    Meaning: Page is INTERACTIVE - users can click, type, interact
 *    Use case: E2E tests waiting for page to be usable
 *    When to set: After initial data is loaded AND UI is mounted and responsive
 *
 * 2. data-screenshot-ready="true" on document.body
 *    Meaning: Page content is loaded and animations have settled
 *    Use case: Marketing screenshot automation
 *    When to set: When visual content is stable (messages loaded, etc.)
 *
 * == PAGE IMPLEMENTATIONS ==
 *
 * Dashboard (/dashboard):
 *   - data-ready: Set when !isLoading (API call complete, UI rendered)
 *
 * Canvas (/canvas):
 *   - data-ready: Set when isWorkflowFetched (workflow data loaded, canvas mounted)
 *
 * Chat (/chat):
 *   - data-ready: Set when chatReady flag is true (app mounted, EventBus ready)
 *   - data-screenshot-ready: Set when messages.length > 0 (content visible for screenshots)
 *
 * == E2E TESTING ==
 *
 * Prefer waitForPageReady() from helpers/ready-signals.ts which checks data-ready.
 * For chat-specific interactive readiness, use waitForReadyFlag(page, 'chatReady').
 *
 * == MARKETING SCREENSHOTS ==
 *
 * Use data-screenshot-ready for capturing pages with content loaded.
 * This ensures animations are complete and content is visible.
 */

/**
 * Helper to set page as interactive-ready
 * Call this when the page is fully mounted and responsive to user input
 */
export function setPageInteractiveReady(): void {
  document.body.setAttribute('data-ready', 'true');
}

/**
 * Helper to clear page interactive-ready state
 * Call this on unmount
 */
export function clearPageInteractiveReady(): void {
  document.body.removeAttribute('data-ready');
}

/**
 * Helper to set page as screenshot-ready
 * Call this when content is loaded and animations have settled
 */
export function setPageScreenshotReady(): void {
  document.body.setAttribute('data-screenshot-ready', 'true');
}

/**
 * Helper to clear page screenshot-ready state
 * Call this on unmount
 */
export function clearPageScreenshotReady(): void {
  document.body.removeAttribute('data-screenshot-ready');
}
