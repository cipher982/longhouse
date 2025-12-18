import { describe, it, expect, beforeEach, vi, type Mock } from 'vitest'
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

// Mock fetch for SSE
const mockFetch = vi.fn()
global.fetch = mockFetch

describe('SupervisorChatController (Typing Indicator Option C)', () => {
  let controller: SupervisorChatController

  beforeEach(() => {
    vi.clearAllMocks()
    controller = new SupervisorChatController()
  })

  it('sends clientCorrelationId in request body', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? 'text/event-stream' : null) },
      body: new ReadableStream({
        start(c) {
          c.close();
        }
      }),
    })

    await controller.sendMessage('Hello', 'test-id')

    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('/chat'),
      expect.objectContaining({
        body: JSON.stringify({ message: 'Hello', client_correlation_id: 'test-id' }),
      })
    )
  })

  it('updates status to typing on connected event with correlationId', async () => {
    const correlationId = 'test-id'
    const sseData = `event: connected\ndata: ${JSON.stringify({ run_id: 123, client_correlation_id: correlationId })}\n\n`
    const encoder = new TextEncoder()

    mockFetch.mockResolvedValueOnce({
      ok: true,
      headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? 'text/event-stream' : null) },
      body: new ReadableStream({
        start(c) {
          c.enqueue(encoder.encode(sseData));
          c.close();
        }
      }),
    })

    await controller.sendMessage('Hello', correlationId)

    expect(stateManager.updateAssistantStatus).toHaveBeenCalledWith(correlationId, 'typing')
  })

  it('updates status to canceled when new message interrupts active stream', async () => {
    const firstCorrelationId = 'first-id'
    const secondCorrelationId = 'second-id'

    // Setup first call to stay "active"
    let firstCallController: ReadableStreamDefaultController;
    const firstCallStream = new ReadableStream({
      start(c) { firstCallController = c; }
    });

    mockFetch.mockImplementationOnce(() => {
      return Promise.resolve({
        ok: true,
        headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? 'text/event-stream' : null) },
        body: firstCallStream,
      })
    })

    // Setup second call to finish immediately
    mockFetch.mockResolvedValueOnce({
      ok: true,
      headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? 'text/event-stream' : null) },
      body: new ReadableStream({
        start(c) { c.close(); }
      }),
    })

    // Start first send (don't await yet as it blocks)
    const p1 = controller.sendMessage('First', firstCorrelationId)

    // Send second message
    await controller.sendMessage('Second', secondCorrelationId)

    // Should have canceled the first one
    expect(stateManager.updateAssistantStatus).toHaveBeenCalledWith(firstCorrelationId, 'canceled')

    // Cleanup first call
    firstCallController!.close();
    await p1
  })

  it('maps supervisor_complete to final status and streams result', async () => {
    const correlationId = 'test-id'
    const resultText = 'Finished result'
    const sseData = `event: supervisor_complete\ndata: ${JSON.stringify({
      type: 'supervisor_complete',
      payload: { result: resultText },
      client_correlation_id: correlationId
    })}\n\n`
    const encoder = new TextEncoder()

    mockFetch.mockResolvedValueOnce({
      ok: true,
      headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? 'text/event-stream' : null) },
      body: new ReadableStream({
        start(c) {
          c.enqueue(encoder.encode(sseData));
          c.close();
        }
      }),
    })

    await controller.sendMessage('Hello', correlationId)

    // Verify it started streaming with the correlationId
    expect(conversationController.startStreaming).toHaveBeenCalledWith(correlationId)

    // Verify it updated to final state
    expect(stateManager.updateAssistantStatus).toHaveBeenCalledWith(correlationId, 'final', resultText)
  })

  it('handles terminal error by updating status to error', async () => {
    const correlationId = 'test-id'
    const errorMsg = 'Backend failure'
    const sseData = `event: error\ndata: ${JSON.stringify({
      type: 'error',
      payload: { error: errorMsg },
      client_correlation_id: correlationId
    })}\n\n`
    const encoder = new TextEncoder()

    mockFetch.mockResolvedValueOnce({
      ok: true,
      headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? 'text/event-stream' : null) },
      body: new ReadableStream({
        start(c) {
          c.enqueue(encoder.encode(sseData));
          c.close();
        }
      }),
    })

    await controller.sendMessage('Hello', correlationId)

    expect(stateManager.updateAssistantStatus).toHaveBeenCalledWith(correlationId, 'error')
    expect(stateManager.showToast).toHaveBeenCalledWith(expect.stringContaining(errorMsg), 'error')
  })
})
