import { randomUUID } from 'crypto';
import type { Page, APIRequestContext } from '@playwright/test';
import { test, expect } from './fixtures';

const BENIGN_CONSOLE_PATTERNS = [
  /Download the React DevTools/,
  /\[HMR\]/,
  /Failed to load resource.*favicon/i,
  /Content Security Policy/,
];

function attachErrorCollectors(page: Page): { consoleErrors: string[]; serverErrors: string[] } {
  const consoleErrors: string[] = [];
  const serverErrors: string[] = [];

  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      const text = msg.text();
      if (!BENIGN_CONSOLE_PATTERNS.some((pattern) => pattern.test(text))) {
        consoleErrors.push(text);
      }
    }
  });

  page.on('response', (response) => {
    const url = response.url();
    const status = response.status();
    if (url.includes('/api/') && (status >= 500 || (status >= 400 && status !== 401))) {
      serverErrors.push(`${status} ${url}`);
    }
  });

  return { consoleErrors, serverErrors };
}

async function assertNoRuntimeErrors(page: Page, label: string, consoleErrors: string[], serverErrors: string[]) {
  if (serverErrors.length > 0) {
    throw new Error(`${label}: server errors: ${serverErrors.join(', ')}`);
  }
  if (consoleErrors.length > 0) {
    throw new Error(`${label}: console errors: ${consoleErrors.join(' | ')}`);
  }
}

type IngestSessionOverrides = Partial<{
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
}>;

async function ingestSession(request: APIRequestContext, overrides: IngestSessionOverrides = {}): Promise<string> {
  const sessionId = overrides.id || randomUUID();
  const timestamp = overrides.started_at || new Date().toISOString();

  const response = await request.post('/api/agents/ingest', {
    data: {
      id: sessionId,
      provider: overrides.provider || 'claude',
      environment: overrides.environment || 'e2e-machine',
      project: overrides.project || `live-lineage-${sessionId.slice(0, 8)}`,
      device_id: 'live-lineage-e2e',
      cwd: '/tmp',
      git_repo: null,
      git_branch: null,
      provider_session_id: overrides.provider_session_id || `live-lineage-${sessionId}`,
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
            content_text: 'hello from live lineage test',
            timestamp,
            source_path: '/tmp/live-lineage.jsonl',
            source_offset: 0,
          },
        ],
    },
  });

  expect(response.ok(), `ingest failed: ${response.status()} ${await response.text()}`).toBe(true);
  return sessionId;
}

test('live thread card groups continuations and stale branch stays explicit', async ({ agentsRequest, context }) => {
  test.setTimeout(45_000);

  const project = `live-lineage-${randomUUID().slice(0, 8)}`;
  const rootId = await ingestSession(agentsRequest, {
    provider: 'claude',
    project,
    environment: 'Cinder',
    events: [
      {
        role: 'user',
        content_text: 'Started on laptop for live lineage proof',
        timestamp: new Date().toISOString(),
        source_path: '/tmp/live-lineage-root.jsonl',
        source_offset: 0,
      },
    ],
  });

  const childTimestamp = new Date(Date.now() + 60_000).toISOString();
  const childId = await ingestSession(agentsRequest, {
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
        content_text: 'Continued in cloud for live lineage proof',
        timestamp: childTimestamp,
        source_path: '/tmp/live-lineage-cloud.jsonl',
        source_offset: 0,
      },
    ],
  });

  const page = await context.newPage();
  const { consoleErrors, serverErrors } = attachErrorCollectors(page);

  await page.goto(`/timeline?project=${project}`, { waitUntil: 'domcontentloaded' });

  const card = page.locator('.session-card', { hasText: project });
  await expect(card).toHaveCount(1, { timeout: 15_000 });
  await expect(card).toContainText('Head: Cloud');
  await expect(card).toContainText('Started: Cinder');
  await expect(card).toContainText('2 continuations');

  await card.click();
  await expect(page).toHaveURL(`/timeline/${childId}`);
  await expect(page.getByTestId('session-lineage-panel')).toBeVisible();
  await expect(page.getByTestId('session-branch-banner')).toHaveCount(0);

  await page.goto(`/timeline/${rootId}`, { waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('session-branch-banner')).toContainText('not the latest continuation');
  await expect(page.getByTestId('session-continuation-panel')).toContainText(
    'Branch from this point in cloud',
  );
  await expect(page.getByRole('button', { name: 'Branch in Cloud' })).toBeVisible();

  await assertNoRuntimeErrors(page, 'live lineage detail', consoleErrors, serverErrors);
  await page.close();
});

test('live search keeps one card but opens the matching older continuation', async ({ agentsRequest, context }) => {
  test.setTimeout(45_000);

  const project = `live-lineage-search-${randomUUID().slice(0, 8)}`;
  const token = `live-branch-token-${randomUUID().slice(0, 8)}`;
  const rootId = await ingestSession(agentsRequest, {
    provider: 'claude',
    project,
    environment: 'Cinder',
    events: [
      {
        role: 'user',
        content_text: `Original laptop branch contains ${token}`,
        timestamp: new Date().toISOString(),
        source_path: '/tmp/live-lineage-search-root.jsonl',
        source_offset: 0,
      },
    ],
  });

  const childTimestamp = new Date(Date.now() + 60_000).toISOString();
  await ingestSession(agentsRequest, {
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
        content_text: 'Cloud continuation without the root search token',
        timestamp: childTimestamp,
        source_path: '/tmp/live-lineage-search-cloud.jsonl',
        source_offset: 0,
      },
    ],
  });

  const page = await context.newPage();
  const { consoleErrors, serverErrors } = attachErrorCollectors(page);

  await page.goto(`/timeline?project=${project}`, { waitUntil: 'domcontentloaded' });
  const searchInput = page.locator('input[type="search"]');
  await searchInput.fill(token);
  await expect(page).toHaveURL(new RegExp(`query=${token}`));

  const card = page.locator('.session-card', { hasText: project });
  await expect(card).toHaveCount(1, { timeout: 15_000 });
  await expect(card.locator('.session-card-snippet')).toContainText(token);

  await card.click();
  await expect(page).toHaveURL(new RegExp(`/timeline/${rootId}.*event_id=`));
  await expect(page.getByTestId('session-branch-banner')).toBeVisible();

  await assertNoRuntimeErrors(page, 'live lineage search', consoleErrors, serverErrors);
  await page.close();
});
