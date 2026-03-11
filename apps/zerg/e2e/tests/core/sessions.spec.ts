/**
 * Sessions Timeline E2E Tests
 *
 * Tests the agent sessions list and detail pages.
 * Note: These tests require sessions to be present in the database.
 * In a fresh test DB, the empty state will be shown.
 */

import { randomUUID } from 'crypto';
import type { APIRequestContext } from '@playwright/test';
import { test, expect, type Page } from '../fixtures';

async function ensureDemoProviders(page: Page): Promise<void> {
  // Hero empty state has no toolbar — seed demos first if visible
  const heroEmpty = page.locator('.sessions-hero-empty');
  if (await heroEmpty.isVisible({ timeout: 2000 }).catch(() => false)) {
    const loadDemo = page.getByRole('button', { name: /Load demo/i });
    await loadDemo.click();
    await page.waitForSelector('.sessions-toolbar', { timeout: 15000 });
  }

  // Open filter popover to check available providers
  const filterBtn = page.locator('button[aria-controls="filter-panel"]');
  if (await filterBtn.isVisible()) {
    const filterPanel = page.locator('#filter-panel');
    if (!(await filterPanel.isVisible().catch(() => false))) {
      await filterBtn.click();
    }
  }

  const claudeOption = page.locator('[data-filter-section="provider"] [data-filter-option="claude"]');
  const hasClaude = await claudeOption.count();
  if (hasClaude > 0) {
    return;
  }

  // Fallback: try loading demos if provider not yet available
  const loadDemo = page.getByRole('button', { name: /Load demo/i });
  if (await loadDemo.isVisible()) {
    await loadDemo.click();
  }

  await expect(claudeOption).toHaveCount(1, { timeout: 15000 });
}

async function ensureFilterPanelOpen(page: Page): Promise<void> {
  const filterPanel = page.locator('#filter-panel');
  if (!(await filterPanel.isVisible().catch(() => false))) {
    await page.locator('button[aria-controls="filter-panel"]').click();
    await expect(filterPanel).toBeVisible({ timeout: 5000 });
  }
}

async function ingestSession(
  request: APIRequestContext,
  overrides: Partial<{
    id: string;
    provider: string;
    project: string;
    environment: string;
    provider_session_id: string;
    thread_root_session_id: string;
    continued_from_session_id: string;
    continuation_kind: string;
    origin_label: string;
    branched_from_event_id: number;
    started_at: string;
    ended_at: string;
    events: Array<{
      role: string;
      content_text: string;
      timestamp: string;
      source_path: string;
      source_offset: number;
    }>;
  }> = {},
): Promise<string> {
  const sessionId = overrides.id || randomUUID();
  const timestamp = overrides.started_at || new Date().toISOString();

  const ingest = await request.post('/api/agents/ingest', {
    data: {
      id: sessionId,
      provider: overrides.provider || 'claude',
      environment: overrides.environment || 'e2e-machine',
      project: overrides.project || 'sessions-e2e',
      device_id: 'e2e-device',
      cwd: '/tmp',
      git_repo: null,
      git_branch: null,
      provider_session_id: overrides.provider_session_id || `claude-session-${sessionId}`,
      thread_root_session_id: overrides.thread_root_session_id,
      continued_from_session_id: overrides.continued_from_session_id,
      continuation_kind: overrides.continuation_kind,
      origin_label: overrides.origin_label,
      branched_from_event_id: overrides.branched_from_event_id,
      started_at: timestamp,
      ended_at: overrides.ended_at || timestamp,
      events:
        overrides.events || [
          {
            role: 'user',
            content_text: 'hello',
            timestamp,
            source_path: '/tmp/session.jsonl',
            source_offset: 0,
          },
        ],
    },
  });

  expect(ingest.ok()).toBe(true);
  return sessionId;
}

test.describe('Sessions Page', () => {
  test('Sessions tab renders and shows list or empty state', async ({ page }) => {
    // Navigate to timeline (sessions)
    await page.goto('/timeline');

    // Wait for page to be ready
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // The header nav should be visible with Sessions tab
    await expect(page.locator('.header-nav')).toBeVisible();
    await expect(page.locator('.nav-tab:has-text("Timeline")')).toBeVisible();

    // Should show either sessions list or hero empty state
    const hasSessions = await page.locator('.session-card').count() > 0;
    const hasHeroEmpty = await page.locator('.sessions-hero-empty').isVisible();

    expect(hasSessions || hasHeroEmpty).toBe(true);
  });

  test('Filter bar is visible and interactive', async ({ page }) => {
    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Seed demos first so toolbar is visible (hero state has no toolbar)
    await ensureDemoProviders(page);

    // Toolbar should be visible
    const toolbar = page.locator('.sessions-toolbar');
    await expect(toolbar).toBeVisible();

    // Search input should be present on the toolbar
    await expect(toolbar.locator('input[type="search"]')).toBeVisible();

    // Filter toggle should be present
    await expect(page.locator('button[aria-controls="filter-panel"]')).toBeVisible();

    // Filter popover should be open (ensureDemoProviders opened it)
    const filterPanel = page.locator('#filter-panel');
    await expect(filterPanel).toBeVisible();
    await expect(filterPanel.locator('[data-filter-section]').first()).toBeVisible();
  });

  test('Filter by provider updates URL', async ({ page }) => {
    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    await ensureDemoProviders(page);
    const claudeBtn = page.locator('[data-filter-section="provider"] [data-filter-option="claude"]');
    await claudeBtn.click();

    // URL should update with provider param
    await expect(page).toHaveURL(/provider=claude/);
  });

  test('Search input triggers debounced query', async ({ page }) => {
    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // If hero state, seed demos first so search input is on toolbar
    const heroEmpty = page.locator('.sessions-hero-empty');
    if (await heroEmpty.isVisible({ timeout: 2000 }).catch(() => false)) {
      await page.getByRole('button', { name: /Load demo/i }).click();
      await page.waitForSelector('.sessions-toolbar', { timeout: 15000 });
    }

    // Type in search
    const searchInput = page.locator('input[type="search"]');
    await searchInput.fill('test query');

    // URL should include query param (auto-polls for debounce)
    await expect(page).toHaveURL(/query=test\+query|query=test%20query/);
  });

  test('Search results show snippet and jump to matching event', async ({ page, request }) => {
    const sessionId = randomUUID();
    const timestamp = new Date().toISOString();
    const magicToken = 'krypton-needle';

    const ingest = await request.post('/api/agents/ingest', {
      data: {
        id: sessionId,
        provider: 'claude',
        environment: 'development',
        project: 'fts-e2e',
        device_id: 'e2e-device',
        cwd: '/tmp',
        git_repo: null,
        git_branch: null,
        started_at: timestamp,
        events: [
          {
            role: 'user',
            content_text: `Find ${magicToken} in this session`,
            timestamp,
            source_path: '/tmp/session.jsonl',
            source_offset: 0,
          },
        ],
      },
    });

    expect(ingest.ok()).toBe(true);

    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    const searchInput = page.locator('input[type="search"]');
    await searchInput.fill(magicToken);
    await expect(page).toHaveURL(new RegExp(`query=${magicToken}`));

    const sessionCard = page.locator('.session-card', { hasText: 'fts-e2e' }).first();
    await expect(sessionCard).toBeVisible();

    const snippet = sessionCard.locator('.session-card-snippet');
    await expect(snippet).toContainText(magicToken);
    await expect(snippet.locator('mark.search-highlight')).toBeVisible();

    await sessionCard.click();

    await expect(page).toHaveURL(new RegExp(`/timeline/${sessionId}.*event_id=`));
    await page.waitForSelector('body[data-ready="true"]', { timeout: 10000 });
    const highlight = page.locator('.event-highlight');
    const highlightedCount = await highlight.count();
    if (highlightedCount > 0) {
      await expect(highlight).toContainText(magicToken, { timeout: 15000 });
    } else {
      const matchedEvent = page.getByTestId('session-timeline-row').filter({ hasText: magicToken }).first();
      await expect(matchedEvent).toBeVisible({ timeout: 15000 });
    }
  });

  test('Clear filters button removes all filters', async ({ page }) => {
    // Navigate with pre-set filters — filtersOpen auto-opens from URL params
    await page.goto('/timeline?provider=claude&project=zerg');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Clear button should be visible
    const clearButton = page.getByRole('button', { name: 'Clear', exact: true });
    await expect(clearButton).toBeVisible();

    // Click clear
    await clearButton.click();

    // URL should no longer have filter params
    await expect(page).toHaveURL('/timeline');
  });
});

test.describe('Filter Chips and Popover', () => {
  test('selecting a filter creates a chip in the toolbar', async ({ page }) => {
    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });
    await ensureDemoProviders(page);

    // Select provider filter via popover
    await page.locator('[data-filter-section="provider"] [data-filter-option="claude"]').click();

    // Chip should appear in the toolbar
    const chip = page.locator('.sessions-filter-chip', { hasText: 'claude' });
    await expect(chip).toBeVisible();
  });

  test('dismissing a chip clears the filter and removes the chip', async ({ page }) => {
    await page.goto('/timeline?provider=claude');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Chip should be visible
    const chip = page.locator('.sessions-filter-chip', { hasText: 'claude' });
    await expect(chip).toBeVisible();

    // Click the dismiss button
    await chip.locator('.sessions-filter-chip-dismiss').click();

    // Chip should be gone and URL cleared
    await expect(chip).toHaveCount(0);
    await expect(page).toHaveURL('/timeline');
  });

  test('multiple active filters show multiple chips', async ({ page, request }) => {
    const machineName = `e2e-multichip-${randomUUID().slice(0, 8)}`;
    await ingestSession(request, { environment: machineName, project: 'multichip-e2e', provider: 'claude' });

    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });
    await ensureDemoProviders(page);

    // Select provider
    await page.locator('[data-filter-section="provider"] [data-filter-option="claude"]').click();

    // Open popover again to select machine
    await ensureFilterPanelOpen(page);
    const machineBtn = page.locator(`[data-filter-section="machine"] [data-filter-option="${machineName}"]`);
    await expect(machineBtn).toHaveCount(1, { timeout: 8000 });
    await machineBtn.click();

    // Both chips should be visible
    await expect(page.locator('.sessions-filter-chip', { hasText: 'claude' })).toBeVisible();
    await expect(page.locator('.sessions-filter-chip', { hasText: machineName })).toBeVisible();

    // URL should have both params
    await expect(page).toHaveURL(/provider=claude/);
    await expect(page).toHaveURL(new RegExp(`environment=${machineName}`));
  });

  test('Escape closes the filter popover', async ({ page }) => {
    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });
    await ensureDemoProviders(page);

    // Popover is open (ensureDemoProviders opened it)
    await expect(page.locator('#filter-panel')).toBeVisible();

    // Press Escape
    await page.keyboard.press('Escape');

    await expect(page.locator('#filter-panel')).toHaveCount(0);
  });

  test('clicking outside the popover closes it', async ({ page }) => {
    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });
    await ensureDemoProviders(page);

    await expect(page.locator('#filter-panel')).toBeVisible();

    // Click the page title (far from the popover and filter button)
    await page.locator('.ui-section-header__title').click();

    await expect(page.locator('#filter-panel')).toHaveCount(0);
  });

  test('non-default days filter creates a chip', async ({ page }) => {
    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });
    await ensureDemoProviders(page);

    // Select 30d in the popover
    const days30 = page.locator('[data-filter-section="time"] [data-filter-option="30d"]');
    await days30.scrollIntoViewIfNeeded();
    await days30.evaluate((el) => {
      (el as HTMLButtonElement).click();
    });

    // Chip should appear
    const chip = page.locator('.sessions-filter-chip', { hasText: '30d' });
    await expect(chip).toBeVisible();

    // URL should update
    await expect(page).toHaveURL(/days_back=30/);
  });

  test('filter button badge shows active filter count', async ({ page }) => {
    await page.goto('/timeline?provider=claude&days_back=30');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Filter button badge should show 2
    const badge = page.locator('button[aria-controls="filter-panel"] .sessions-filter-badge');
    await expect(badge).toBeVisible();
    await expect(badge).toHaveText('2');
  });
});

test.describe('Session Detail Page', () => {
  test('Shows error for invalid session ID', async ({ page }) => {
    // Navigate to a non-existent session
    await page.goto('/timeline/00000000-0000-0000-0000-000000000000');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Should show error state
    await expect(page.locator('.ui-empty-state')).toBeVisible();
    await expect(page.locator('text=Error loading session')).toBeVisible();

    // Back button should be visible
    const backButton = page.locator('button:has-text("Back")');
    await expect(backButton).toBeVisible();
  });

  test('Back button navigates to sessions list', async ({ page }) => {
    // Navigate to invalid session to get error state
    await page.goto('/timeline/00000000-0000-0000-0000-000000000000');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Click back button
    await page.locator('button:has-text("Back")').click();

    // Should be back on sessions list
    await expect(page).toHaveURL('/timeline');
  });

  test('Claude legacy resume links still land on the transcript first', async ({ page, request }) => {
    const sessionId = await ingestSession(request, {
      provider: 'claude',
      project: 'resume-e2e',
      provider_session_id: 'resume-session-e2e',
    });

    await page.goto(`/timeline/${sessionId}?resume=1`);
    await page.waitForSelector('body[data-ready="true"]', { timeout: 10000 });

    await expect(page).toHaveURL(`/timeline/${sessionId}`);
    await expect(page.getByTestId('session-timeline-header')).toContainText('Event Timeline');
    await expect(page.getByRole('button', { name: 'Continue in Cloud' })).toBeVisible();
    await expect(page.getByTestId('session-continuation-panel')).toHaveCount(0);
    await expect(page.getByTestId('session-timeline-list')).toBeVisible();
  });


  test('Claude continuation send creates a cloud branch instead of crashing', async ({ page, request }) => {
    const project = `resume-send-${randomUUID().slice(0, 8)}`;
    const rootId = await ingestSession(request, {
      provider: 'claude',
      project,
      environment: 'Cinder',
      provider_session_id: `resume-send-${randomUUID().slice(0, 8)}`,
      events: [
        {
          role: 'user',
          content_text: 'Started on laptop before cloud continuation',
          timestamp: new Date().toISOString(),
          source_path: '/tmp/session.jsonl',
          source_offset: 0,
        },
      ],
    });

    await page.goto(`/timeline/${rootId}?resume=1`);
    await page.waitForSelector('body[data-ready="true"]', { timeout: 10000 });

    await page.getByRole('button', { name: 'Continue in Cloud' }).click();
    const composer = page.locator('.session-chat-composer textarea');
    await composer.fill('anything else?');
    await page.getByRole('button', { name: 'Send' }).click();

    await expect(page.locator('.session-chat-error')).toHaveCount(0);
    await expect(page.locator('.session-chat-message--user')).toContainText('anything else?');

    await expect
      .poll(() => page.url(), { timeout: 10000 })
      .not.toContain(`/timeline/${rootId}`);

    await expect(page.getByTestId('session-branch-banner')).toHaveCount(0);

    const sessionsResp = await request.get(`/api/agents/sessions?project=${project}&days_back=1`);
    expect(sessionsResp.ok()).toBe(true);
    const sessionsPayload = await sessionsResp.json();
    const threadCard = sessionsPayload.sessions.find((session: {
      thread_root_session_id?: string;
      id: string;
      thread_continuation_count?: number;
      thread_head_session_id?: string;
    }) => {
      return (session.thread_root_session_id || session.id) === rootId;
    });
    expect(threadCard).toBeTruthy();
    expect(threadCard.thread_continuation_count).toBe(2);
    expect(threadCard.thread_head_session_id).not.toBe(rootId);
  });


  test('Timeline groups continuations into one task card and opens the latest head', async ({ page, request }) => {
    const project = `thread-group-${randomUUID().slice(0, 8)}`;
    const rootId = await ingestSession(request, {
      provider: 'claude',
      project,
      environment: 'Cinder',
      events: [
        {
          role: 'user',
          content_text: 'Started on laptop',
          timestamp: new Date().toISOString(),
          source_path: '/tmp/session.jsonl',
          source_offset: 0,
        },
      ],
    });

    const childTimestamp = new Date(Date.now() + 60_000).toISOString();
    const childId = await ingestSession(request, {
      provider: 'claude',
      project,
      environment: 'cloud-runtime',
      thread_root_session_id: rootId,
      continued_from_session_id: rootId,
      continuation_kind: 'cloud',
      origin_label: 'Cloud',
      started_at: childTimestamp,
      ended_at: childTimestamp,
      events: [
        {
          role: 'user',
          content_text: 'Continued in cloud',
          timestamp: childTimestamp,
          source_path: '/tmp/session-cloud.jsonl',
          source_offset: 0,
        },
      ],
    });

    await page.goto(`/timeline?project=${project}`);
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    const card = page.locator('.session-card', { hasText: project });
    await expect(card).toHaveCount(1);
    await expect(card).toContainText('Head: Cloud');
    await expect(card).toContainText('Started: Cinder');
    await expect(card).toContainText('2 continuations');

    await card.click();
    await expect(page).toHaveURL(`/timeline/${childId}`);
    await expect(page.getByTestId('session-lineage-panel')).toBeVisible();
    await expect(page.getByTestId('session-branch-banner')).toHaveCount(0);
  });

  test('Older branches show a stale banner and branch-from-here continuation copy', async ({ page, request }) => {
    const project = `thread-branch-${randomUUID().slice(0, 8)}`;
    const rootId = await ingestSession(request, {
      provider: 'claude',
      project,
      environment: 'Cinder',
      events: [
        {
          role: 'user',
          content_text: 'Laptop origin branch',
          timestamp: new Date().toISOString(),
          source_path: '/tmp/session.jsonl',
          source_offset: 0,
        },
      ],
    });

    const childTimestamp = new Date(Date.now() + 60_000).toISOString();
    const childId = await ingestSession(request, {
      provider: 'claude',
      project,
      environment: 'cloud-runtime',
      thread_root_session_id: rootId,
      continued_from_session_id: rootId,
      continuation_kind: 'cloud',
      origin_label: 'Cloud',
      started_at: childTimestamp,
      ended_at: childTimestamp,
      events: [
        {
          role: 'user',
          content_text: 'Cloud head branch',
          timestamp: childTimestamp,
          source_path: '/tmp/session-cloud.jsonl',
          source_offset: 0,
        },
      ],
    });

    await page.goto(`/timeline/${rootId}`);
    await page.waitForSelector('body[data-ready="true"]', { timeout: 10000 });

    await expect(page.getByTestId('session-branch-banner')).toContainText('not the latest continuation');
    await expect(page.getByTestId('session-lineage-panel')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Branch from Here' })).toBeVisible();
    await page.getByRole('button', { name: 'Branch from Here' }).click();
    await expect(page.getByTestId('session-continuation-panel')).toBeVisible();
    await expect(page.locator('.session-chat-empty')).toContainText('branch from this history');

    await page.getByRole('button', { name: 'Open Latest' }).focus();
    await page.keyboard.press('Enter');
    await expect(page).toHaveURL(`/timeline/${childId}`);
    await expect(page.getByTestId('session-branch-banner')).toHaveCount(0);
  });

  test('Search keeps one thread card but opens the matching older continuation', async ({ page, request }) => {
    const project = `thread-search-${randomUUID().slice(0, 8)}`;
    const token = `branch-token-${randomUUID().slice(0, 8)}`;
    const rootId = await ingestSession(request, {
      provider: 'claude',
      project,
      environment: 'Cinder',
      events: [
        {
          role: 'user',
          content_text: `Original laptop branch contains ${token}`,
          timestamp: new Date().toISOString(),
          source_path: '/tmp/session.jsonl',
          source_offset: 0,
        },
      ],
    });

    const childTimestamp = new Date(Date.now() + 60_000).toISOString();
    await ingestSession(request, {
      provider: 'claude',
      project,
      environment: 'cloud-runtime',
      thread_root_session_id: rootId,
      continued_from_session_id: rootId,
      continuation_kind: 'cloud',
      origin_label: 'Cloud',
      started_at: childTimestamp,
      ended_at: childTimestamp,
      events: [
        {
          role: 'user',
          content_text: 'Cloud continuation without the search token',
          timestamp: childTimestamp,
          source_path: '/tmp/session-cloud.jsonl',
          source_offset: 0,
        },
      ],
    });

    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    const searchInput = page.locator('input[type="search"]');
    await searchInput.fill(token);
    await expect(page).toHaveURL(new RegExp(`query=${token}`));

    const card = page.locator('.session-card', { hasText: project });
    await expect(card).toHaveCount(1);
    await expect(card.locator('.session-card-snippet')).toContainText(token);

    await card.click();
    await expect(page).toHaveURL(new RegExp(`/timeline/${rootId}.*event_id=`));
    await expect(page.getByTestId('session-branch-banner')).toBeVisible();
  });

  test('Non-Claude sessions explain the cloud continuation gap explicitly', async ({ page, request }) => {
    const sessionId = await ingestSession(request, {
      provider: 'codex',
      project: 'resume-hidden-e2e',
    });

    await page.goto(`/timeline/${sessionId}`);
    await page.waitForSelector('body[data-ready="true"]', { timeout: 10000 });

    await expect(page.getByRole('button', { name: 'Latest Context' })).toHaveCount(0);
    await expect(page.getByTestId('session-continuation-panel')).toHaveCount(0);
    await expect(page.getByTestId('session-continuation-unavailable')).toContainText('wired for Claude sessions only');
    await expect(page.locator('.session-chat-composer textarea')).toHaveCount(0);
  });

  test('Opening a long session lands near the latest continuation point instead of the top', async ({ page, request }) => {
    const sessionId = randomUUID();
    const now = Date.now();
    const events = Array.from({ length: 80 }, (_, idx) => {
      const timestamp = new Date(now + idx * 1000).toISOString();
      return {
        role: idx % 2 === 0 ? 'user' : 'assistant',
        content_text: `Latest-context event ${idx + 1}`,
        timestamp,
        source_path: '/tmp/session.jsonl',
        source_offset: idx,
      };
    });

    const ingest = await request.post('/api/agents/ingest', {
      data: {
        id: sessionId,
        provider: 'claude',
        environment: 'development',
        project: 'latest-context-e2e',
        device_id: 'e2e-device',
        cwd: '/tmp',
        git_repo: null,
        git_branch: null,
        provider_session_id: 'latest-context-session-e2e',
        started_at: new Date(now).toISOString(),
        ended_at: new Date(now + 79_000).toISOString(),
        events,
      },
    });

    expect(ingest.ok()).toBe(true);

    await page.goto(`/timeline/${sessionId}`);
    await page.waitForSelector('body[data-ready="true"]', { timeout: 10000 });

    const timelineList = page.getByTestId('session-timeline-list');
    await expect
      .poll(async () => timelineList.evaluate((el) => el.scrollTop), { timeout: 4000 })
      .toBeGreaterThan(0);

    await expect(page.getByRole('button', { name: 'Continue in Cloud' })).toBeVisible();
    await expect(page.getByTestId('session-continuation-panel')).toHaveCount(0);
  });

  test('desktop workspace panes can be resized and persisted', async ({ page, request }) => {
    const sessionId = randomUUID();
    const now = Date.now();
    const ingest = await request.post('/api/agents/ingest', {
      data: {
        id: sessionId,
        provider: 'claude',
        environment: 'development',
        project: 'workspace-resize-e2e',
        device_id: 'e2e-device',
        cwd: '/tmp',
        git_repo: null,
        git_branch: null,
        started_at: new Date(now).toISOString(),
        ended_at: new Date(now + 3000).toISOString(),
        events: [
          {
            role: 'user',
            content_text: 'Show me a resizable workspace.',
            timestamp: new Date(now).toISOString(),
            source_path: '/tmp/session.jsonl',
            source_offset: 0,
          },
          {
            role: 'assistant',
            content_text: 'I will inspect the repo and report back.',
            timestamp: new Date(now + 1000).toISOString(),
            source_path: '/tmp/session.jsonl',
            source_offset: 1,
          },
          {
            role: 'assistant',
            tool_name: 'Bash',
            tool_input_json: { command: 'ls -la' },
            tool_call_id: 'toolu_resize_workspace',
            timestamp: new Date(now + 2000).toISOString(),
            source_path: '/tmp/session.jsonl',
            source_offset: 2,
          },
          {
            role: 'tool',
            tool_name: 'Bash',
            tool_output_text: 'README.md\nsrc/\npackage.json',
            tool_call_id: 'toolu_resize_workspace',
            timestamp: new Date(now + 3000).toISOString(),
            source_path: '/tmp/session.jsonl',
            source_offset: 3,
          },
        ],
      },
    });

    expect(ingest.ok()).toBe(true);

    await page.goto('/timeline');
    await page.waitForSelector('body[data-ready="true"]', { timeout: 10000 });
    await page.evaluate(() => {
      window.localStorage.removeItem('zerg:session-workspace-layout:v1');
    });

    await page.goto(`/timeline/${sessionId}`);
    await page.waitForSelector('body[data-ready="true"]', { timeout: 10000 });

    const sidebarPane = page.locator('.workspace-shell__pane--sidebar');
    const mainPane = page.locator('.workspace-shell__pane--main');
    const sidebarHandle = page.getByTestId('session-workspace-sidebar-resize');

    const sidebarBefore = await sidebarPane.boundingBox();
    const mainBefore = await mainPane.boundingBox();
    const sidebarHandleBox = await sidebarHandle.boundingBox();
    expect(sidebarBefore).toBeTruthy();
    expect(mainBefore).toBeTruthy();
    expect(sidebarHandleBox).toBeTruthy();

    await page.mouse.move(
      (sidebarHandleBox?.x ?? 0) + (sidebarHandleBox?.width ?? 0) / 2,
      (sidebarHandleBox?.y ?? 0) + (sidebarHandleBox?.height ?? 0) / 2,
    );
    await page.mouse.down();
    await page.mouse.move(
      (sidebarHandleBox?.x ?? 0) + (sidebarHandleBox?.width ?? 0) / 2 + 80,
      (sidebarHandleBox?.y ?? 0) + (sidebarHandleBox?.height ?? 0) / 2,
      { steps: 12 },
    );
    await page.mouse.up();

    await expect
      .poll(async () => (await sidebarPane.boundingBox())?.width ?? 0, { timeout: 4000 })
      .toBeGreaterThan((sidebarBefore?.width ?? 0) + 40);
    await expect
      .poll(async () => (await mainPane.boundingBox())?.width ?? 0, { timeout: 4000 })
      .toBeLessThan((mainBefore?.width ?? 0) - 20);

    const storedLayout = await page.evaluate(() =>
      JSON.parse(window.localStorage.getItem('zerg:session-workspace-layout:v1') || '{}'),
    );
    expect(storedLayout.sidebarWidth).toBeGreaterThan(280);

    await page.reload();
    await page.waitForSelector('body[data-ready="true"]', { timeout: 10000 });
    await expect
      .poll(async () => (await page.locator('.workspace-shell__pane--sidebar').boundingBox())?.width ?? 0, {
        timeout: 4000,
      })
      .toBeGreaterThan((sidebarBefore?.width ?? 0) + 40);

    await page.getByTestId('session-workspace-sidebar-resize').focus();
    await page.keyboard.press('Enter');

    await expect
      .poll(async () => (await page.locator('.workspace-shell__pane--sidebar').boundingBox())?.width ?? 0, {
        timeout: 4000,
      })
      .toBeLessThan(storedLayout.sidebarWidth - 20);

    await page.locator('[data-row-kind="tool"]').first().click();

    const inspectorPane = page.locator('.workspace-shell__pane--inspector');
    const inspectorHandle = page.getByTestId('session-workspace-inspector-resize');
    await expect(inspectorPane).toBeVisible();

    const inspectorBefore = await inspectorPane.boundingBox();
    const inspectorHandleBox = await inspectorHandle.boundingBox();
    expect(inspectorBefore).toBeTruthy();
    expect(inspectorHandleBox).toBeTruthy();

    await page.mouse.move(
      (inspectorHandleBox?.x ?? 0) + (inspectorHandleBox?.width ?? 0) / 2,
      (inspectorHandleBox?.y ?? 0) + (inspectorHandleBox?.height ?? 0) / 2,
    );
    await page.mouse.down();
    await page.mouse.move(
      (inspectorHandleBox?.x ?? 0) + (inspectorHandleBox?.width ?? 0) / 2 - 80,
      (inspectorHandleBox?.y ?? 0) + (inspectorHandleBox?.height ?? 0) / 2,
      { steps: 12 },
    );
    await page.mouse.up();

    await expect
      .poll(async () => (await inspectorPane.boundingBox())?.width ?? 0, { timeout: 4000 })
      .toBeGreaterThan((inspectorBefore?.width ?? 0) + 40);

    const storedInspectorLayout = await page.evaluate(() =>
      JSON.parse(window.localStorage.getItem('zerg:session-workspace-layout:v1') || '{}'),
    );
    expect(storedInspectorLayout.inspectorWidth).toBeGreaterThan(360);

    await page.reload();
    await page.waitForSelector('body[data-ready="true"]', { timeout: 10000 });
    await page.locator('[data-row-kind="tool"]').first().click();

    await expect
      .poll(async () => (await page.locator('.workspace-shell__pane--inspector').boundingBox())?.width ?? 0, {
        timeout: 4000,
      })
      .toBeGreaterThan((inspectorBefore?.width ?? 0) + 40);
  });

  test('scrolls from left and right gutters on timeline detail', async ({ page, request }) => {
    const sessionId = randomUUID();
    const now = Date.now();
    const events = Array.from({ length: 80 }, (_, idx) => {
      const timestamp = new Date(now + idx * 1000).toISOString();
      return {
        role: idx % 2 === 0 ? 'user' : 'assistant',
        content_text: `Scroll regression event ${idx + 1}`,
        timestamp,
        source_path: '/tmp/session.jsonl',
        source_offset: idx,
      };
    });

    const ingest = await request.post('/api/agents/ingest', {
      data: {
        id: sessionId,
        provider: 'claude',
        environment: 'development',
        project: 'scroll-gutter-e2e',
        device_id: 'e2e-device',
        cwd: '/tmp',
        git_repo: null,
        git_branch: null,
        started_at: new Date(now).toISOString(),
        ended_at: new Date(now + 79_000).toISOString(),
        events,
      },
    });

    expect(ingest.ok()).toBe(true);

    await page.goto(`/timeline/${sessionId}`);
    await page.waitForSelector('body[data-ready="true"]', { timeout: 10000 });
    const timelineList = page.getByTestId('session-timeline-list');
    await expect(page.getByTestId('session-timeline-row').first()).toBeVisible();

    await expect(timelineList).toBeVisible();

    await timelineList.evaluate((el) => {
      el.scrollTop = 0;
    });
    await expect.poll(async () => timelineList.evaluate((el) => el.scrollTop)).toBeLessThan(20);

    const startTop = await timelineList.evaluate((el) => el.scrollTop);
    const box = await timelineList.boundingBox();
    expect(box).toBeTruthy();
    const paneHeight = box?.height ?? 0;
    const paneWidth = box?.width ?? 0;
    const gutterY = Math.max(40, Math.floor(paneHeight * 0.5));
    const leftX = Math.min(Math.max(24, Math.floor(paneWidth * 0.08)), Math.max(24, paneWidth - 24));
    const rightX = Math.max(24, paneWidth - 24);

    // Wheel from the far-left edge of the timeline pane.
    await timelineList.hover({ position: { x: leftX, y: gutterY } });
    await page.mouse.wheel(0, 600);

    await expect
      .poll(async () => timelineList.evaluate((el) => el.scrollTop), { timeout: 4000 })
      .toBeGreaterThan(startTop);

    const afterLeft = await timelineList.evaluate((el) => el.scrollTop);

    // Wheel from the far-right edge of the timeline pane.
    await timelineList.hover({ position: { x: rightX, y: gutterY } });
    await page.mouse.wheel(0, 600);

    await expect
      .poll(async () => timelineList.evaluate((el) => el.scrollTop), { timeout: 4000 })
      .toBeGreaterThan(afterLeft);
  });
});

test.describe('Sessions Navigation', () => {
  test('Sessions tab in nav links to /sessions', async ({ page }) => {
    await page.goto('/dashboard');
    await page.waitForSelector('.header-nav', { timeout: 10000 });

    // Click Timeline tab
    await page.locator('.nav-tab:has-text("Timeline")').click();

    // Should navigate to timeline page
    await expect(page).toHaveURL('/timeline');
  });
});

test.describe('Machine Filter', () => {
  test('filters API returns machines list', async ({ request }) => {
    // Ingest sessions with distinct machine names
    const machineA = `e2e-machine-a-${randomUUID().slice(0, 8)}`;
    const machineB = `e2e-machine-b-${randomUUID().slice(0, 8)}`;

    await ingestSession(request, { environment: machineA, project: 'machine-filter-e2e' });
    await ingestSession(request, { environment: machineB, project: 'machine-filter-e2e' });

    const resp = await request.get('/api/agents/filters?days_back=1');
    expect(resp.ok()).toBe(true);

    const data = await resp.json();
    expect(data).toHaveProperty('machines');
    expect(Array.isArray(data.machines)).toBe(true);
    expect(data.machines).toContain(machineA);
    expect(data.machines).toContain(machineB);
  });

  test('machine filter dropdown appears in filter panel', async ({ page, request }) => {
    const machineName = `e2e-machine-${randomUUID().slice(0, 8)}`;
    await ingestSession(request, { environment: machineName, project: 'machine-ui-e2e' });

    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });
    await ensureDemoProviders(page);

    // Filter popover should have a machine section with the ingested machine name
    const filterPanel = page.locator('#filter-panel');
    const machineSection = filterPanel.locator('[data-filter-section="machine"]');
    await expect(machineSection).toBeVisible({ timeout: 8000 });
  });

  test('selecting a machine updates the URL', async ({ page, request }) => {
    const machineName = `e2e-select-${randomUUID().slice(0, 8)}`;
    await ingestSession(request, { environment: machineName, project: 'machine-url-e2e' });

    await page.goto('/timeline');
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });
    await ensureDemoProviders(page);

    const filterPanel = page.locator('#filter-panel');

    // Wait for the machine option button to appear (filters poll from API)
    const machineBtn = filterPanel.locator(`[data-filter-section="machine"] [data-filter-option="${machineName}"]`);
    await expect(machineBtn).toHaveCount(1, { timeout: 10000 });

    await machineBtn.click();
    await expect(page).toHaveURL(new RegExp(`environment=${machineName}`));
  });

  test('machine filter shows only sessions from that machine', async ({ page, request }) => {
    const machineToken = `filter-machine-${randomUUID().slice(0, 8)}`;
    const otherToken = `other-machine-${randomUUID().slice(0, 8)}`;

    await ingestSession(request, { environment: machineToken, project: 'machine-filter-sessions' });
    await ingestSession(request, { environment: otherToken, project: 'machine-filter-other' });

    // Navigate with machine filter pre-applied
    await page.goto(`/timeline?environment=${machineToken}`);
    await page.waitForSelector('[data-ready="true"]', { timeout: 10000 });

    // Should show the filtered machine's sessions
    const cards = page.locator('.session-card');
    await expect(cards.first()).toBeVisible({ timeout: 10000 });

    // All visible cards should belong to the filtered machine project
    const allCards = await cards.all();
    for (const card of allCards) {
      const text = await card.textContent();
      // The project name is visible on each card
      expect(text).toContain('machine-filter-sessions');
    }
  });
});
