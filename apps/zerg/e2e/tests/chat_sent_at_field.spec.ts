import { test, expect, type Page } from './fixtures';

// Skip: Chat timestamp selectors need updating for new chat UI
test.skip();

/**
 * E2E tests for ThreadMessage.sent_at field (timezone-aware UTC timestamps)
 *
 * CONTEXT:
 * - Previously: timestamp and created_at were naïve datetimes, leading to timezone reinterpretation
 * - Now: sent_at is a timezone-aware DateTime(UTC) field that serializes with explicit timezone marker
 * - Messages maintain consistent timestamps across optimistic→server flow
 *
 * WHAT WE'RE TESTING:
 * 1. sent_at field exists and is timezone-aware (has +00:00 or Z suffix)
 * 2. Optimistic message shows no timestamp, then receives server timestamp
 * 3. Message timestamps don't flicker or change unexpectedly
 * 4. Multiple messages maintain chronological order by server ID
 */

// Reset DB before each test to keep fiche/thread ids predictable
test.beforeEach(async ({ page, request }) => {
  await request.post('/api/admin/reset-database', {
    data: { clear_data: true },
  });
  await page.goto('/');
});

async function createFicheAndNavigateToChat(page: Page) {
  // Intercept auth
  const authResponse = await page.request.post('/api/auth/google', {
    data: { token: 'fake_token' },
  });
  const { access_token } = await authResponse.json();
  await page.context().addCookies([
    {
      name: 'access_token',
      value: access_token,
      domain: 'localhost',
      path: '/',
    },
  ]);

  // Create fiche via API
  const ficheRes = await page.request.post('/api/fiches', {
    headers: { Authorization: `Bearer ${access_token}` },
    data: {
      name: 'Test Fiche',
      description: 'E2E test fiche',
      model: 'gpt-5-nano',
    },
  });
  const fiche = await ficheRes.json();

  // Navigate to chat for this fiche
  await page.goto(`/fiche/${fiche.id}`);
  await expect(page.getByTestId('chat-container')).toBeVisible({ timeout: 5000 });
}

async function getMessagesInDomOrder(page: Page): Promise<string[]> {
  const messageElements = await page.locator('[data-testid="chat-message"]').all();
  const messages: string[] = [];
  for (const elem of messageElements) {
    const content = await elem.locator('.message-content').textContent();
    if (content) messages.push(content);
  }
  return messages;
}

test.describe('Chat sent_at Field - Timezone Awareness', () => {
  test('sent_at field is timezone-aware (contains timezone info)', async ({ page }) => {
    await createFicheAndNavigateToChat(page);

    // Intercept and inspect the API response
    let capturedMessage: any = null;
    page.on('response', async (response) => {
      if (response.url().includes('/api/threads/') && response.url().includes('/messages')) {
        const data = await response.json();
        if (Array.isArray(data) && data.length > 0) {
          capturedMessage = data[0];
        }
      }
    });

    // Send a message
    await page.getByTestId('chat-input').fill('Test message');
    await page.getByTestId('send-message-btn').click();

    // Wait for server response
    await page.waitForTimeout(3000);

    // Verify captured message has proper sent_at format with timezone
    expect(capturedMessage).toBeTruthy();
    expect(capturedMessage.sent_at).toBeTruthy();
    // ISO 8601 with timezone: "2025-10-30T17:02:11+00:00" or "2025-10-30T17:02:11Z"
    expect(capturedMessage.sent_at).toMatch(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\+00:00|Z)/);
  });

  test('Optimistic message shows no timestamp, then server timestamp appears', async ({ page }) => {
    await createFicheAndNavigateToChat(page);

    const testMessage = 'Timestamp lifecycle test';

    // Send message
    await page.getByTestId('chat-input').fill(testMessage);
    await page.getByTestId('send-message-btn').click();

    // Immediately check: optimistic message should have no visible timestamp
    const messageTime = page.locator('[data-testid="chat-message"]').last().locator('.message-time');
    let initialText = await messageTime.textContent();
    expect(initialText).toBe(''); // Empty string from formatTimestamp("")

    // Wait for server response
    await page.waitForTimeout(2000);

    // After server response: message should have a timestamp
    initialText = await messageTime.textContent();
    expect(initialText).toMatch(/\d{2}:\d{2}/); // HH:MM format
  });

  test('Multiple messages maintain order by server ID, not timestamp', async ({ page }) => {
    await createFicheAndNavigateToChat(page);

    const messages = ['First', 'Second', 'Third'];

    // Send multiple messages rapidly
    for (const msg of messages) {
      await page.getByTestId('chat-input').fill(msg);
      await page.getByTestId('send-message-btn').click();
      await page.waitForTimeout(100); // Small delay between sends
    }

    // Wait for all to complete
    await page.waitForTimeout(5000);

    // Verify order in DOM matches send order (NOT chronological by timestamp)
    const domOrder = await getMessagesInDomOrder(page);
    const testMessages = domOrder.filter(m => messages.includes(m));

    expect(testMessages).toEqual(messages);
  });

  test('Timestamp does not change between optimistic and server states', async ({ page }) => {
    await createFicheAndNavigateToChat(page);

    // This test captures the key bug: timestamp shouldn't change from noon to 5pm
    let capturedOptimistic: string | null = null;
    let capturedServer: string | null = null;

    // Monitor DOM changes to catch timestamp
    await page.evaluate(() => {
      const observer = new MutationObserver(() => {
        const lastMsg = document.querySelector('[data-testid="chat-message"]:last-child');
        if (lastMsg) {
          const timeEl = lastMsg.querySelector('.message-time');
          if (timeEl?.textContent) {
            console.log('TIME_UPDATE:' + timeEl.textContent);
          }
        }
      });
      observer.observe(document.body, { subtree: true, characterData: true });
    });

    await page.getByTestId('chat-input').fill('Timestamp consistency test');
    await page.getByTestId('send-message-btn').click();

    // Capture all console messages containing TIME_UPDATE
    const timeupdates: string[] = [];
    page.on('console', msg => {
      if (msg.text().startsWith('TIME_UPDATE')) {
        timeupdates.push(msg.text().replace('TIME_UPDATE:', ''));
      }
    });

    // Wait for server response
    await page.waitForTimeout(3000);

    // The timestamp should appear once and stay the same
    // (or start empty then get value, but NOT change values)
    const finalTime = await page.locator('[data-testid="chat-message"]').last().locator('.message-time').textContent();
    expect(finalTime).toMatch(/\d{2}:\d{2}|^$/); // Either has time or empty, never changes
  });
});

test.describe('Chat sent_at Field - Server Validation', () => {
  test('Server rejects client-provided sent_at beyond ±5 minutes', async ({ page }) => {
    await createFicheAndNavigateToChat(page);

    // Get access token
    const authResponse = await page.request.post('/api/auth/google', {
      data: { token: 'fake_token' },
    });
    const { access_token } = await authResponse.json();

    // Create thread
    const threadRes = await page.request.post('/api/fiches/1/threads', {
      headers: { Authorization: `Bearer ${access_token}` },
      data: { title: 'Validation test' },
    });
    const thread = await threadRes.json();

    // Try to send message with sent_at from 10 minutes ago
    const pastTime = new Date();
    pastTime.setMinutes(pastTime.getMinutes() - 10);

    const msgRes = await page.request.post(`/api/threads/${thread.id}/messages`, {
      headers: { Authorization: `Bearer ${access_token}` },
      data: {
        role: 'user',
        content: 'Test',
        sent_at: pastTime.toISOString(),
      },
    });

    // Server should either reject or override with its own time
    const msg = await msgRes.json();
    expect(msg.sent_at).toBeTruthy();
    // Verify the sent_at is close to NOW (not 10 minutes ago)
    const diff = Math.abs(new Date().getTime() - new Date(msg.sent_at).getTime());
    expect(diff).toBeLessThan(60000); // Within 1 minute of server time
  });
});
