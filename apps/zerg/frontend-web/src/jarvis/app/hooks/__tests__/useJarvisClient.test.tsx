import React from 'react';
import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';

import { AppProvider } from '../../context/AppContext';
import { useJarvisClient } from '../useJarvisClient';

type EventHandlers = {
  onConnected?: () => void;
  onError?: (error: Event) => void;
};

const handlersRef: { current: EventHandlers | null } = { current: null };

const mockClient = vi.hoisted(() => ({
  isAuthenticated: vi.fn().mockResolvedValue(false),
  connectEventStream: vi.fn((handlers: EventHandlers) => {
    handlersRef.current = handlers;
  }),
  disconnectEventStream: vi.fn(),
  listAgents: vi.fn().mockResolvedValue([
    { id: 1, name: 'Agent One', status: 'idle' },
  ]),
}));

vi.mock('../../../core', () => ({
  getJarvisClient: () => mockClient,
}));

function wrapper({ children }: { children: React.ReactNode }) {
  return <AppProvider>{children}</AppProvider>;
}

beforeEach(() => {
  handlersRef.current = null;
  mockClient.isAuthenticated.mockResolvedValue(false);
  mockClient.connectEventStream.mockClear();
  mockClient.disconnectEventStream.mockClear();
  mockClient.listAgents.mockClear();
});

describe('useJarvisClient', () => {
  it('connect waits for SSE connected event before setting isConnected', async () => {
    const { result } = renderHook(() => useJarvisClient({ autoConnect: false }), { wrapper });

    await act(async () => {
      await result.current.connect();
    });

    expect(mockClient.connectEventStream).toHaveBeenCalledTimes(1);
    expect(result.current.isConnected).toBe(false);

    await act(async () => {
      handlersRef.current?.onConnected?.();
    });

    expect(result.current.isConnected).toBe(true);
  });

  it('fetchAgents requests fresh data and updates cache', async () => {
    const { result } = renderHook(() => useJarvisClient({ autoConnect: false }), { wrapper });

    await act(async () => {
      await result.current.initialize();
    });

    let agents: Array<{ id: number; name: string; status: string }> = [];
    await act(async () => {
      agents = await result.current.fetchAgents();
    });

    expect(mockClient.listAgents).toHaveBeenCalledTimes(1);
    expect(agents).toEqual([{ id: 1, name: 'Agent One', status: 'idle' }]);
    expect(result.current.agents).toEqual([{ id: 1, name: 'Agent One', status: 'idle' }]);
  });
});
