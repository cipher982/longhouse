import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { OikosChatController } from '../oikos-chat-controller';

describe('OikosChatController history loading', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('requests web-scoped history by default', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ messages: [], total: 0 }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const controller = new OikosChatController();
    await controller.loadHistory(25);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain('/api/oikos/history?');
    expect(url).toContain('limit=25');
    expect(url).toContain('surface_id=web');
    expect(url).not.toContain('view=all');
    expect(options.credentials).toBe('include');
  });

  it('requests all-activity view when specified', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ messages: [], total: 0 }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const controller = new OikosChatController();
    await controller.loadHistory(50, { view: 'all' });

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain('/api/conversations/activity?');
    expect(url).not.toContain('/api/oikos/history');
  });

  it('maps surface metadata fields from history payload', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        messages: [
          {
            role: 'assistant',
            content: 'Done',
            timestamp: '2026-03-04T12:00:00Z',
            origin_surface_id: 'telegram',
            delivery_surface_id: 'telegram',
            visibility: 'surface-local',
          },
        ],
        total: 1,
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const controller = new OikosChatController();
    const messages = await controller.loadHistory(50);

    expect(messages).toHaveLength(1);
    expect(messages[0].origin_surface_id).toBe('telegram');
    expect(messages[0].delivery_surface_id).toBe('telegram');
    expect(messages[0].visibility).toBe('surface-local');
  });

  it('loads canonical conversation messages when a conversation id is provided', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        messages: [
          {
            role: 'assistant',
            content: 'Done from canonical conversation',
            sent_at: '2026-03-12T15:00:00Z',
            message_metadata: {
              surface: {
                origin_surface_id: 'web',
                delivery_surface_id: 'web',
                visibility: 'surface-local',
              },
              usage: {
                total_tokens: 42,
              },
              oikos: {
                tool_calls: [
                  {
                    id: 'call_123',
                    name: 'search_conversations',
                    args: { q: 'Portugal' },
                  },
                ],
              },
            },
          },
        ],
        total: 1,
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const controller = new OikosChatController();
    const messages = await controller.loadHistory(50, { conversation_id: 17 });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain('/api/conversations/17/messages?');
    expect(url).not.toContain('/api/oikos/history');
    expect(messages).toHaveLength(1);
    expect(messages[0].origin_surface_id).toBe('web');
    expect(messages[0].usage?.total_tokens).toBe(42);
    expect(messages[0].tool_calls?.[0].tool_name).toBe('search_conversations');
  });

  it('loads all-activity from canonical conversation activity', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        messages: [
          {
            role: 'assistant',
            content: 'Telegram follow-up',
            sent_at: '2026-03-12T15:00:00Z',
            message_metadata: {
              surface: {
                origin_surface_id: 'telegram',
                delivery_surface_id: 'telegram',
                visibility: 'cross-surface',
              },
            },
          },
        ],
        total: 1,
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const controller = new OikosChatController();
    const messages = await controller.loadHistory(50, { view: 'all' });

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain('/api/conversations/activity?');
    expect(messages).toHaveLength(1);
    expect(messages[0].origin_surface_id).toBe('telegram');
    expect(messages[0].visibility).toBe('cross-surface');
  });
});
