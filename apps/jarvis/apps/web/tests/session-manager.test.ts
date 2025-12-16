import { describe, it, expect, beforeEach } from 'vitest';
import { SessionManager } from '@jarvis/core';
import type { SyncTransport } from '@jarvis/data-local';

const stubTransportFactory = (calls: string[]): SyncTransport => {
  return async (input, init) => {
    calls.push(input);

    if (input.includes('/sync/push')) {
      const body = init?.body ? JSON.parse(init.body) : { ops: [] };
      const acked = Array.isArray(body.ops) ? body.ops.map((op: any) => op.opId).filter(Boolean) : [];
      return {
        ok: true,
        status: 200,
        json: async () => ({ acked, nextCursor: 1 })
      } as any;
    }

    if (input.includes('/sync/pull')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ ops: [], nextCursor: 1 })
      } as any;
    }

    throw new Error(`Unexpected sync call to ${input}`);
  };
};

describe('SessionManager sync configuration', () => {
  beforeEach(() => {
    // Reset IndexedDB between tests via fake-indexeddb/auto setup
  });

  it('pushes and pulls using the configured sync base URL', async () => {
    const calls: string[] = [];
    const transport = stubTransportFactory(calls);

    const sessionManager = new SessionManager({}, {
      conversationManagerOptions: {
        syncBaseUrl: 'http://localhost:47300/api/jarvis',
        syncTransport: transport
      },
      maxHistoryTurns: 10
    });

    await sessionManager.initializeSession({ name: 'Test Context' }, 'test');

    await sessionManager.addConversationTurn({
      id: 'turn-1',
      timestamp: new Date(),
      userTranscript: 'Hello'
    });

    await sessionManager.flush();

    sessionManager.configureSync({
      syncBaseUrl: 'https://sync.example.com/api',
      syncTransport: transport
    });

    const result = await sessionManager.syncNow();

    expect(result).toEqual({ pushed: 1, pulled: 0 });
    expect(calls).toHaveLength(2);
    expect(calls[0]).toBe('https://sync.example.com/api/sync/push');
    expect(calls[1]).toBe('https://sync.example.com/api/sync/pull?cursor=1');

    await sessionManager.endSession();
  });
});
