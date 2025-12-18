import { describe, it, expect, beforeEach, afterEach, vi, type Mock } from 'vitest'
import { SupervisorChatController } from '../lib/supervisor-chat-controller'
import { stateManager } from '../lib/state-manager'
import { conversationController } from '../lib/conversation-controller'

// Mock state-manager
vi.mock('../lib/state-manager', () => ({
  stateManager: {
    updateAssistantStatus: vi.fn(),
    showToast: vi.fn(),
    setStreamingText: vi.fn(),
    finalizeMessage: vi.fn(),
  },
}))

// Mock conversation-controller
vi.mock('../lib/conversation-controller', () => ({
  conversationController: {
    startStreaming: vi.fn(),
    appendStreaming: vi.fn(),
    finalizeStreaming: vi.fn(),
  },
}))

describe('SupervisorChatController (Typing Indicator Option C)', () => {
  let controller: SupervisorChatController
  let fetchMock: Mock

  beforeEach(() => {
    vi.clearAllMocks()
    vi.useFakeTimers()

    // Create a fresh fetch mock for each test
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    controller = new SupervisorChatController()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  const createMockResponse = (bodyData?: string | Uint8Array) => {
    const encoder = new TextEncoder()
    const stream = new ReadableStream({
      start(c) {
        if (bodyData) {
          const value = typeof bodyData === 'string' ? encoder.encode(bodyData) : bodyData
          c.enqueue(value)
        }
        c.close()
      }
    })

    return {
      ok: true,
      status: 200,
      statusText: 'OK',
      headers: new Headers({ 'content-type': 'text/event-stream' }),
      body: stream,
    } as any as Response
  }

  it('sends clientCorrelationId in request body', async () => {
    fetchMock.mockResolvedValueOnce(createMockResponse())

    await controller.sendMessage('Hello', 'test-id')

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/chat'),
      expect.objectContaining({
        body: JSON.stringify({ message: 'Hello', client_correlation_id: 'test-id' }),
      })
    )
  })

  it('updates status to typing on connected event with correlationId', async () => {
    const correlationId = 'test-id'
    const sseData = `event: connected\ndata: ${JSON.stringify({ run_id: 123, client_correlation_id: correlationId })}\n\n`

    fetchMock.mockResolvedValueOnce(createMockResponse(sseData))

    await controller.sendMessage('Hello', correlationId)

    expect(stateManager.updateAssistantStatus).toHaveBeenCalledWith(correlationId, 'typing')
  })

  it('updates status to canceled when new message interrupts active stream', async () => {
    const firstCorrelationId = 'first-id'
    const secondCorrelationId = 'second-id'

    // Setup first call to stay "active"
    let firstCallResolve: any
    const firstCallPromise = new Promise(resolve => { firstCallResolve = resolve })

    fetchMock.mockImplementationOnce(() => {
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'text/event-stream' }),
        body: {
          getReader: () => ({
            read: () => firstCallPromise,
            releaseLock: () => {},
          }),
        },
      } as any as Response)
    })

    // Setup second call to finish immediately
    fetchMock.mockResolvedValueOnce(createMockResponse())

    // Start first send
    const p1 = controller.sendMessage('First', firstCorrelationId)

    // Send second message
    await controller.sendMessage('Second', secondCorrelationId)

    // Should have canceled the first one
    expect(stateManager.updateAssistantStatus).toHaveBeenCalledWith(firstCorrelationId, 'canceled')

    // Cleanup
    firstCallResolve({ done: true })
    try { await p1 } catch (e) {}
  })

  it('handles terminal error by updating status to error', async () => {
    const correlationId = 'test-id'
    const errorMsg = 'Backend failure'
    const sseData = `event: error\ndata: ${JSON.stringify({
      type: 'error',
      payload: { error: errorMsg },
      client_correlation_id: correlationId
    })}\n\n`

    fetchMock.mockResolvedValueOnce(createMockResponse(sseData))

    await controller.sendMessage('Hello', correlationId)

    expect(stateManager.updateAssistantStatus).toHaveBeenCalledWith(correlationId, 'error')
    expect(stateManager.showToast).toHaveBeenCalledWith(expect.stringContaining(errorMsg), 'error')
  })

  it('watchdog triggers error status after silence', async () => {
    const correlationId = 'timeout-id'

    fetchMock.mockImplementationOnce(() => {
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'text/event-stream' }),
        body: {
          getReader: () => ({
            read: () => new Promise(() => {}), // Never resolve
            releaseLock: () => {},
          }),
        },
      } as any as Response)
    })

    const sendPromise = controller.sendMessage('Hello', correlationId)

    // Advance timers by 61s
    vi.advanceTimersByTime(61000)

    expect(stateManager.updateAssistantStatus).toHaveBeenCalledWith(correlationId, 'error')
    expect(stateManager.showToast).toHaveBeenCalledWith('Timed out waiting for response', 'error')

    // Stream should be aborted and the send should settle.
    await expect(sendPromise).resolves.toBeUndefined()
  })
})
