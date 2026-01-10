import { test, expect, type Page } from './fixtures';

/**
 * AUTOMATION HISTORY E2E TESTS
 *
 * Tests for the collapsible automation history section that displays
 * scheduled and manual automation runs separately from chat threads.
 *
 * Coverage:
 * 1. Collapsible state toggle (expand/collapse)
 * 2. Badge display for scheduled runs (🔄)
 * 3. Badge display for manual runs (▶️)
 * 4. Thread separation (chat threads vs automation threads)
 * 5. Count badge accuracy
 */

test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database', { data: { reset_type: 'clear_data' } });
});

async function createAgentAndGetId(page: Page): Promise<string> {
  await page.goto('/');
  await page.locator('[data-testid="create-agent-btn"]').click();
  const row = page.locator('tr[data-agent-id]').first();
  await expect(row).toBeVisible();
  return (await row.getAttribute('data-agent-id')) as string;
}

test.describe('Automation History Section', () => {
  test('Collapsible section toggles visibility', async ({ page, request }) => {
    console.log('🎯 Testing: Automation history collapse/expand toggle');

    const agentId = await createAgentAndGetId(page);

    // Create a chat thread first to prevent auto-creation
    await request.post('/api/threads', {
      data: {
        agent_id: parseInt(agentId),
        title: 'Chat Thread',
        thread_type: 'chat',
      }
    });

    // Create a scheduled automation run via API
    const response = await request.post('/api/threads', {
      data: {
        agent_id: parseInt(agentId),
        title: 'Scheduled Test Run',
        thread_type: 'scheduled',
      }
    });

    expect(response.status()).toBe(201);
    const thread = await response.json();
    console.log(`📊 Created scheduled thread ID: ${thread.id}`);

    // Navigate to chat to see automation history
    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await page.waitForLoadState('networkidle');
    // Wait for chat interface to be ready (deterministic)

    // Verify automation history section is visible
    await expect(page.locator('[data-testid="automation-history"]')).toBeVisible({ timeout: 5000 });
    console.log('✅ Automation history section visible');

    // Initially should be collapsed (default state)
    const automationList = page.locator('[data-testid="automation-runs-list"]');
    await expect(automationList).toHaveClass(/collapsed/);
    console.log('✅ Initially collapsed');

    // Click header to expand
    await page.locator('[data-testid="automation-history-header"]').click();
    // Animation waits replaced with deterministic expects below
    await expect(automationList).not.toHaveClass(/collapsed/);
    console.log('✅ Expanded successfully');

    // Verify the automation run is visible after expanding
    await expect(page.locator(`[data-testid="automation-run-${thread.id}"]`)).toBeVisible();

    // Click to collapse again
    await page.locator('[data-testid="automation-history-header"]').click();
    // Animation waits replaced with deterministic expects below
    await expect(automationList).toHaveClass(/collapsed/);
    console.log('✅ Collapsed again successfully');
  });

  test('Scheduled runs show correct badge', async ({ page, request }) => {
    console.log('🎯 Testing: Scheduled run badge display');

    const agentId = await createAgentAndGetId(page);

    // Create a chat thread first to prevent auto-creation
    await request.post('/api/threads', {
      data: {
        agent_id: parseInt(agentId),
        title: 'Chat Thread',
        thread_type: 'chat',
      }
    });

    // Create a scheduled run
    const response = await request.post('/api/threads', {
      data: {
        agent_id: parseInt(agentId),
        title: 'Scheduled Automation',
        thread_type: 'scheduled',
      }
    });

    expect(response.status()).toBe(201);
    const thread = await response.json();
    console.log(`📊 Created scheduled thread ID: ${thread.id}`);

    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await page.waitForLoadState('networkidle');
    // Wait for chat interface to be ready (deterministic)

    // Expand automation history
    await page.locator('[data-testid="automation-history-header"]').click();
    // Animation waits replaced with deterministic expects below

    // Verify scheduled badge
    const runItem = page.locator(`[data-testid="automation-run-${thread.id}"]`);
    await expect(runItem).toBeVisible({ timeout: 5000 });
    console.log('✅ Automation run item visible');

    const badge = runItem.locator('[data-testid="run-badge-scheduled"]');
    await expect(badge).toBeVisible();
    await expect(badge).toContainText('🔄 Scheduled');
    console.log('✅ Scheduled badge displays correctly');

    // Verify data attributes
    await expect(runItem).toHaveAttribute('data-thread-type', 'scheduled');
    console.log('✅ Thread type attribute correct');
  });

  test('Manual runs show correct badge', async ({ page, request }) => {
    console.log('🎯 Testing: Manual run badge display');

    const agentId = await createAgentAndGetId(page);

    // Create a chat thread first to prevent auto-creation
    await request.post('/api/threads', {
      data: {
        agent_id: parseInt(agentId),
        title: 'Chat Thread',
        thread_type: 'chat',
      }
    });

    // Create a manual run
    const response = await request.post('/api/threads', {
      data: {
        agent_id: parseInt(agentId),
        title: 'Manual Run',
        thread_type: 'manual',
      }
    });

    expect(response.status()).toBe(201);
    const thread = await response.json();
    console.log(`📊 Created manual thread ID: ${thread.id}`);

    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await page.waitForLoadState('networkidle');
    // Wait for chat interface to be ready (deterministic)

    // Expand automation history
    await page.locator('[data-testid="automation-history-header"]').click();
    // Animation waits replaced with deterministic expects below

    // Verify manual badge
    const runItem = page.locator(`[data-testid="automation-run-${thread.id}"]`);
    await expect(runItem).toBeVisible({ timeout: 5000 });
    console.log('✅ Automation run item visible');

    const badge = runItem.locator('[data-testid="run-badge-manual"]');
    await expect(badge).toBeVisible();
    await expect(badge).toContainText('▶️ Manual');
    console.log('✅ Manual badge displays correctly');

    // Verify data attributes
    await expect(runItem).toHaveAttribute('data-thread-type', 'manual');
    console.log('✅ Thread type attribute correct');
  });

  test('Automation threads separated from chat threads', async ({ page, request }) => {
    console.log('🎯 Testing: Thread type separation');

    const agentId = await createAgentAndGetId(page);

    // Create both types of threads
    const chatResponse = await request.post('/api/threads', {
      data: {
        agent_id: parseInt(agentId),
        title: 'Regular Chat Thread',
        thread_type: 'chat'
      }
    });
    expect(chatResponse.status()).toBe(201);
    const chatThread = await chatResponse.json();
    console.log(`📊 Created chat thread ID: ${chatThread.id}`);

    const scheduledResponse = await request.post('/api/threads', {
      data: {
        agent_id: parseInt(agentId),
        title: 'Scheduled Run',
        thread_type: 'scheduled'
      }
    });
    expect(scheduledResponse.status()).toBe(201);
    const scheduledThread = await scheduledResponse.json();
    console.log(`📊 Created scheduled thread ID: ${scheduledThread.id}`);

    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await page.waitForLoadState('networkidle');
    // Wait for chat interface to be ready (deterministic)

    // Verify chat thread appears in main thread list (not automation section)
    const chatThreads = page.locator('.thread-list .thread-item');
    await expect(chatThreads).toHaveCount(1);
    await expect(page.locator(`[data-testid="thread-row-${chatThread.id}"]`)).toBeVisible();
    console.log('✅ Chat thread in main list');

    // Verify automation section exists
    await expect(page.locator('[data-testid="automation-history"]')).toBeVisible();
    console.log('✅ Automation history section visible');

    // Expand automation runs
    await page.locator('[data-testid="automation-history-header"]').click();
    // Animation waits replaced with deterministic expects below

    // Verify automation runs in separate section
    const automationRuns = page.locator('.automation-run-item');
    await expect(automationRuns).toHaveCount(1);
    await expect(page.locator(`[data-testid="automation-run-${scheduledThread.id}"]`)).toBeVisible();
    console.log('✅ Automation thread in automation section');

    // Verify chat thread is NOT in automation section
    await expect(page.locator(`[data-testid="automation-run-${chatThread.id}"]`)).not.toBeVisible();
    console.log('✅ Chat thread correctly excluded from automation section');
  });

  test('Automation count badge shows correct number', async ({ page, request }) => {
    console.log('🎯 Testing: Automation count badge accuracy');

    const agentId = await createAgentAndGetId(page);

    // Create a chat thread first to prevent auto-creation
    await request.post('/api/threads', {
      data: {
        agent_id: parseInt(agentId),
        title: 'Chat Thread',
        thread_type: 'chat',
      }
    });

    // Create 3 automation runs (mix of scheduled and manual)
    for (let i = 0; i < 3; i++) {
      const response = await request.post('/api/threads', {
        data: {
          agent_id: parseInt(agentId),
          title: `Automation Run ${i + 1}`,
          thread_type: i % 2 === 0 ? 'scheduled' : 'manual',
        }
      });
      expect(response.status()).toBe(201);
    }
    console.log('📊 Created 3 automation threads');

    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await page.waitForLoadState('networkidle');
    // Wait for chat interface to be ready (deterministic)

    // Verify count badge shows 3
    const countBadge = page.locator('[data-testid="automation-count"]');
    await expect(countBadge).toBeVisible();
    await expect(countBadge).toContainText('3');
    console.log('✅ Count badge shows correct number: 3');

    // Expand and verify all 3 runs are present
    await page.locator('[data-testid="automation-history-header"]').click();
    // Animation waits replaced with deterministic expects below

    const automationRuns = page.locator('.automation-run-item');
    await expect(automationRuns).toHaveCount(3);
    console.log('✅ All 3 automation runs visible');
  });

  test('Can select and view automation run', async ({ page, request }) => {
    console.log('🎯 Testing: Selecting automation run navigates to thread');

    const agentId = await createAgentAndGetId(page);

    // Create a chat thread first to prevent auto-creation
    await request.post('/api/threads', {
      data: {
        agent_id: parseInt(agentId),
        title: 'Chat Thread',
        thread_type: 'chat',
      }
    });

    // Create automation run with a message
    const response = await request.post('/api/threads', {
      data: {
        agent_id: parseInt(agentId),
        title: 'Clickable Automation',
        thread_type: 'manual',
      }
    });
    expect(response.status()).toBe(201);
    const thread = await response.json();

    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();
    await page.waitForLoadState('networkidle');
    // Wait for chat interface to be ready (deterministic)

    // Expand automation history
    await page.locator('[data-testid="automation-history-header"]').click();
    // Animation waits replaced with deterministic expects below

    // Click on the automation run
    await page.locator(`[data-testid="automation-run-${thread.id}"]`).click();
    // Wait for chat interface to be ready (deterministic)

    // Verify URL updated to show this thread
    const url = page.url();
    expect(url).toContain(`/thread/${thread.id}`);
    console.log(`✅ URL updated to thread: ${url}`);

    // Verify run is marked as selected
    await expect(page.locator(`[data-testid="automation-run-${thread.id}"]`)).toHaveClass(/selected/);
    console.log('✅ Automation run marked as selected');
  });

  test('Automation section hidden when no automation threads exist', async ({ page, request }) => {
    console.log('🎯 Testing: Automation section hidden when empty');

    const agentId = await createAgentAndGetId(page);

    // Only create a regular chat thread
    await request.post('/api/threads', {
      data: {
        agent_id: parseInt(agentId),
        title: 'Chat Only',
        thread_type: 'chat'
      }
    });

    await page.locator(`[data-testid="chat-agent-${agentId}"]`).click();

    // Wait for chat interface to load (don't use networkidle as it may timeout)
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });

    // Verify automation section is NOT rendered at all
    await expect(page.locator('[data-testid="automation-history"]')).toHaveCount(0);
    console.log('✅ Automation section correctly hidden when empty');

    // Verify chat thread still works
    await expect(page.locator('.thread-list .thread-item')).toHaveCount(1, { timeout: 10000 });
    console.log('✅ Chat threads still display normally');
  });
});
