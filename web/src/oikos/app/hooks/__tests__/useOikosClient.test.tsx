import React from 'react';
import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';

import { AppProvider } from '../../context/AppContext';
import { useOikosClient } from '../useOikosClient';

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
  listTasks: vi.fn().mockResolvedValue([
    { id: 1, name: 'Task One', status: 'idle' },
  ]),
}));

vi.mock('../../../core', () => ({
  getOikosClient: () => mockClient,
}));

function wrapper({ children }: { children: React.ReactNode }) {
  return <AppProvider>{children}</AppProvider>;
}

beforeEach(() => {
  handlersRef.current = null;
  mockClient.isAuthenticated.mockResolvedValue(false);
  mockClient.connectEventStream.mockClear();
  mockClient.disconnectEventStream.mockClear();
  mockClient.listTasks.mockClear();
});

describe('useOikosClient', () => {
  it('connect waits for SSE connected event before setting isConnected', async () => {
    const { result } = renderHook(() => useOikosClient({ autoConnect: false }), { wrapper });

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

  it('fetchTasks requests fresh data and updates cache', async () => {
    const { result } = renderHook(() => useOikosClient({ autoConnect: false }), { wrapper });

    await act(async () => {
      await result.current.initialize();
    });

    let tasks: Array<{ id: number; name: string; status: string }> = [];
    await act(async () => {
      tasks = await result.current.fetchTasks();
    });

    expect(mockClient.listTasks).toHaveBeenCalledTimes(1);
    expect(tasks).toEqual([{ id: 1, name: 'Task One', status: 'idle' }]);
    expect(result.current.tasks).toEqual([{ id: 1, name: 'Task One', status: 'idle' }]);
  });
});
