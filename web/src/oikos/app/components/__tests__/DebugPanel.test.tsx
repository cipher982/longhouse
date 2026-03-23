import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { DebugPanel } from '../DebugPanel'

const mockFetchWithRefresh = vi.hoisted(() => vi.fn())
const mockAppState = vi.hoisted(() => ({
  current: {
    messages: [],
    streamingContent: '',
    voiceStatus: 'idle',
    voiceMode: 'push-to-talk',
  } as {
    messages: Array<{ id?: string }>
    streamingContent: string
    voiceStatus: string
    voiceMode: string
  },
}))

vi.mock('../../../../lib/auth-refresh', () => ({
  fetchWithRefresh: mockFetchWithRefresh,
}))

vi.mock('../../context', () => ({
  useAppState: () => mockAppState.current,
}))

function createResponse(data: unknown) {
  return {
    ok: true,
    json: vi.fn().mockResolvedValue(data),
  }
}

function renderPanel(queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false },
  },
})) {
  return render(
    <QueryClientProvider client={queryClient}>
      <DebugPanel isOpen={true} onToggle={vi.fn()} onReset={vi.fn()} />
    </QueryClientProvider>,
  )
}

describe('DebugPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockAppState.current = {
      messages: [],
      streamingContent: '',
      voiceStatus: 'idle',
      voiceMode: 'push-to-talk',
    }
  })

  it('refreshes thread info when the message count changes', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
      },
    })

    mockFetchWithRefresh
      .mockResolvedValueOnce(
        createResponse({
          thread_id: 1,
          title: 'Thread',
          message_count: 1,
          canonical_conversation: {
            id: 10,
            kind: 'chat',
            external_conversation_id: 'conv-1',
            message_count: 1,
          },
        }),
      )
      .mockResolvedValueOnce(
        createResponse({
          thread_id: 1,
          title: 'Thread',
          message_count: 2,
          canonical_conversation: {
            id: 10,
            kind: 'chat',
            external_conversation_id: 'conv-1',
            message_count: 2,
          },
        }),
      )

    const view = renderPanel(queryClient)

    await waitFor(() => expect(screen.getByTestId('debug-thread-id')).toHaveTextContent('1'))
    await waitFor(() => expect(screen.getByTestId('debug-messages-db')).toHaveTextContent('1'))

    mockAppState.current = {
      ...mockAppState.current,
      messages: [{ id: 'm1' }, { id: 'm2' }],
    }

    view.rerender(
      <QueryClientProvider
        client={queryClient}
      >
        <DebugPanel isOpen={true} onToggle={vi.fn()} onReset={vi.fn()} />
      </QueryClientProvider>,
    )

    await waitFor(() => expect(mockFetchWithRefresh).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByTestId('debug-messages-db')).toHaveTextContent('2'))
  })

  it('refetches thread info after reset completes', async () => {
    const user = userEvent.setup()
    const onReset = vi.fn().mockResolvedValue(undefined)

    mockFetchWithRefresh
      .mockResolvedValueOnce(
        createResponse({
          thread_id: 1,
          title: 'Thread',
          message_count: 1,
        }),
      )
      .mockResolvedValueOnce(
        createResponse({
          thread_id: 2,
          title: 'Thread',
          message_count: 0,
        }),
      )

    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
      },
    })

    render(
      <QueryClientProvider client={queryClient}>
        <DebugPanel isOpen={true} onToggle={vi.fn()} onReset={onReset} />
      </QueryClientProvider>,
    )

    await waitFor(() => expect(screen.getByTestId('debug-thread-id')).toHaveTextContent('1'))

    await user.click(screen.getByRole('button', { name: 'Reset Memory' }))

    await waitFor(() => expect(onReset).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(mockFetchWithRefresh).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByTestId('debug-thread-id')).toHaveTextContent('2'))
  })
})
