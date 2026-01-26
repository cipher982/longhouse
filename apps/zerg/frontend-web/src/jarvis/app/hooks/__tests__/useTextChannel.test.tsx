import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { AppProvider } from '../../context'
import { useTextChannel } from '../useTextChannel'
import { eventBus } from '../../../lib/event-bus'

const initializeMock = vi.fn(() => Promise.resolve())
const sendMessageMock = vi.fn()

vi.mock('../../../lib/concierge-chat-controller', () => {
  return {
    ConciergeChatController: class {
      initialize = initializeMock
      sendMessage = (...args: unknown[]) => sendMessageMock(...args)
    },
  }
})

const wrapper = ({ children }: { children: ReactNode }) => (
  <AppProvider>{children}</AppProvider>
)

const flushInit = async () => {
  await act(async () => {
    await Promise.resolve()
  })
}

describe('useTextChannel', () => {
  beforeEach(() => {
    sendMessageMock.mockReset()
    initializeMock.mockClear()
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      cb(0)
      return 0
    })
    vi.stubGlobal('cancelAnimationFrame', () => {})
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('clears isSending on concierge_complete even if send promise is pending', async () => {
    let resolveSend: (() => void) | undefined
    sendMessageMock.mockImplementation(() => new Promise<void>((resolve) => {
      resolveSend = resolve
    }))

    const { result, unmount } = renderHook(() => useTextChannel(), { wrapper })
    await flushInit()

    await act(async () => {
      void result.current.sendMessage('hello')
    })

    expect(result.current.isSending).toBe(true)

    act(() => {
      eventBus.emit('concierge:complete', {
        courseId: 1,
        result: 'ok',
        status: 'success',
        timestamp: Date.now(),
      })
    })

    expect(result.current.isSending).toBe(false)

    resolveSend?.()
    unmount()
  })

  it('does not clear isSending when an earlier send resolves after a newer send', async () => {
    let resolveFirst: (() => void) | undefined
    let resolveSecond: (() => void) | undefined

    sendMessageMock
      .mockImplementationOnce(() => new Promise<void>((resolve) => {
        resolveFirst = resolve
      }))
      .mockImplementationOnce(() => new Promise<void>((resolve) => {
        resolveSecond = resolve
      }))

    const { result } = renderHook(() => useTextChannel(), { wrapper })
    await flushInit()

    await act(async () => {
      void result.current.sendMessage('first')
    })

    await act(async () => {
      void result.current.sendMessage('second')
    })

    expect(result.current.isSending).toBe(true)

    await act(async () => {
      resolveFirst?.()
    })

    expect(result.current.isSending).toBe(true)

    await act(async () => {
      resolveSecond?.()
    })

    expect(result.current.isSending).toBe(false)
  })
})
