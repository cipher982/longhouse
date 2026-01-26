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
  listFiches: vi.fn().mockResolvedValue([
    { id: 1, name: 'Fiche One', status: 'idle' },
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
  mockClient.listFiches.mockClear();
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

  it('fetchFiches requests fresh data and updates cache', async () => {
    const { result } = renderHook(() => useJarvisClient({ autoConnect: false }), { wrapper });

    await act(async () => {
      await result.current.initialize();
    });

    let fiches: Array<{ id: number; name: string; status: string }> = [];
    await act(async () => {
      fiches = await result.current.fetchFiches();
    });

    expect(mockClient.listFiches).toHaveBeenCalledTimes(1);
    expect(fiches).toEqual([{ id: 1, name: 'Fiche One', status: 'idle' }]);
    expect(result.current.fiches).toEqual([{ id: 1, name: 'Fiche One', status: 'idle' }]);
  });
});
