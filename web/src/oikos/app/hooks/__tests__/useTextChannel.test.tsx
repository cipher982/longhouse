import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { AppProvider } from '../../context'
import { useTextChannel } from '../useTextChannel'
import { eventBus } from '../../../lib/event-bus'

const wrapper = ({ children }: { children: ReactNode }) => (
  <AppProvider>{children}</AppProvider>
)

describe('useTextChannel', () => {
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      cb(0)
      return 0
    })
    vi.stubGlobal('cancelAnimationFrame', () => {})
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('clears isSending on oikos_complete even if send promise is pending', async () => {
    let resolveSend: (() => void) | undefined
    const sendText = vi.fn().mockImplementation(() => new Promise<void>((resolve) => {
      resolveSend = resolve
    }))

    const { result, unmount } = renderHook(() => useTextChannel({ sendText }), { wrapper })

    await act(async () => {
      void result.current.sendMessage('hello')
    })

    expect(result.current.isSending).toBe(true)

    act(() => {
      eventBus.emit('oikos:complete', {
        runId: 1,
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

    const sendText = vi.fn()
      .mockImplementationOnce(() => new Promise<void>((resolve) => {
        resolveFirst = resolve
      }))
      .mockImplementationOnce(() => new Promise<void>((resolve) => {
        resolveSecond = resolve
      }))

    const { result } = renderHook(() => useTextChannel({ sendText }), { wrapper })

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
