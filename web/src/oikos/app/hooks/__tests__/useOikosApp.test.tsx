import React from 'react'
import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AppProvider, useAppDispatch } from '../../context'
import { useOikosApp } from '../useOikosApp'

const mockClient = vi.hoisted(() => ({
  isAuthenticated: vi.fn().mockResolvedValue(false),
}))

const mockFetchWithRefresh = vi.hoisted(() => vi.fn())

const mockContextLoader = vi.hoisted(() => ({
  autoDetectContext: vi.fn().mockResolvedValue('personal'),
  loadContext: vi.fn().mockResolvedValue({ name: 'personal' }),
}))

const controllerMocks = vi.hoisted(() => {
  const sendMessage = vi.fn().mockResolvedValue(undefined)
  const initialize = vi.fn().mockResolvedValue(undefined)
  const loadHistory = vi.fn().mockResolvedValue([])
  const attachToRun = vi.fn().mockResolvedValue(undefined)
  const clearHistory = vi.fn().mockResolvedValue(undefined)

  class MockOikosChatController {
    initialize = initialize
    loadHistory = loadHistory
    attachToRun = attachToRun
    clearHistory = clearHistory
    sendMessage = sendMessage
  }

  return {
    MockOikosChatController,
    sendMessage,
    initialize,
    loadHistory,
    attachToRun,
    clearHistory,
  }
})

vi.mock('../../../core', () => ({
  logger: {
    debug: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
  },
  getOikosClient: () => mockClient,
}))

vi.mock('../../../contexts/context-loader', () => ({
  contextLoader: mockContextLoader,
}))

vi.mock('../../../lib/oikos-chat-controller', () => ({
  OikosChatController: controllerMocks.MockOikosChatController,
}))

vi.mock('../../../../lib/auth-refresh', () => ({
  fetchWithRefresh: mockFetchWithRefresh,
}))

function wrapper({ children }: { children: React.ReactNode }) {
  return <AppProvider>{children}</AppProvider>
}

function useHarness() {
  const oikosApp = useOikosApp()
  const dispatch = useAppDispatch()
  return { oikosApp, dispatch }
}

describe('useOikosApp', () => {
  beforeEach(() => {
    vi.clearAllMocks()

    mockClient.isAuthenticated.mockResolvedValue(false)
    mockContextLoader.autoDetectContext.mockResolvedValue('personal')
    mockContextLoader.loadContext.mockResolvedValue({ name: 'personal' })

    controllerMocks.sendMessage.mockResolvedValue(undefined)
    controllerMocks.initialize.mockResolvedValue(undefined)
    controllerMocks.loadHistory.mockResolvedValue([])
    controllerMocks.attachToRun.mockResolvedValue(undefined)
    controllerMocks.clearHistory.mockResolvedValue(undefined)

    mockFetchWithRefresh.mockImplementation(async (url: string) => {
      if (url.includes('/api/oikos/bootstrap')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            prompt: 'You are Oikos.',
            enabled_tools: [],
            user_context: {},
            available_models: [
              {
                id: 'bootstrap-model',
                display_name: 'Bootstrap Model',
                description: 'Bootstrap model',
                capabilities: { reasoning: true, reasoningNone: true },
              },
            ],
            preferences: {
              chat_model: 'bootstrap-model',
              reasoning_effort: 'low',
            },
          }),
        }
      }

      if (url.includes('/api/oikos/runs/active')) {
        return {
          ok: true,
          status: 204,
        }
      }

      throw new Error(`Unexpected fetchWithRefresh call: ${url}`)
    })
  })

  it('uses the latest React preferences for typed text sends', async () => {
    const { result } = renderHook(() => useHarness(), { wrapper })

    await waitFor(() => {
      expect(result.current.oikosApp.initialized).toBe(true)
    })

    act(() => {
      result.current.dispatch({ type: 'UPDATE_PREFERENCE', key: 'chat_model', value: 'live-model' })
      result.current.dispatch({ type: 'UPDATE_PREFERENCE', key: 'reasoning_effort', value: 'high' })
    })

    await act(async () => {
      await result.current.oikosApp.sendText('hello', 'msg-123')
    })

    expect(controllerMocks.sendMessage).toHaveBeenCalledWith('hello', 'msg-123', {
      model: 'live-model',
      reasoning_effort: 'high',
    })
  })
})
