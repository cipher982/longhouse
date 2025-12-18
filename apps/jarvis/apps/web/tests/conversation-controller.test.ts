import { describe, it, expect, beforeEach, vi, type Mock } from 'vitest'
import { ConversationController } from '../lib/conversation-controller'

vi.mock('../lib/state-manager', () => ({
  stateManager: {
    setStreamingText: vi.fn(),
    finalizeMessage: vi.fn(),
  },
}))

import { stateManager } from '../lib/state-manager'

describe('ConversationController (server SSOT)', () => {
  let controller: ConversationController
  let listener: Mock

  beforeEach(() => {
    vi.clearAllMocks()
    controller = new ConversationController()
    listener = vi.fn()
    controller.addListener(listener)
  })

  it('initializes with null conversationId', () => {
    expect(controller.getConversationId()).toBeNull()
  })

  it('can set conversationId and notify listeners', () => {
    controller.setConversationId('server')
    expect(controller.getConversationId()).toBe('server')
    expect(listener).toHaveBeenCalledWith({ type: 'conversationIdChange', id: 'server' })
  })

  it('addUserTurn is a no-op kept for compatibility', async () => {
    await expect(controller.addUserTurn('Hello')).resolves.toBe(true)
  })

  it('streams text and finalizes into stateManager', async () => {
    controller.startStreaming()
    controller.appendStreaming('Hello')
    controller.appendStreaming(' world')

    expect(stateManager.setStreamingText).toHaveBeenCalledWith('Hello')
    expect(stateManager.setStreamingText).toHaveBeenCalledWith('Hello world')

    await controller.finalizeStreaming()

    expect(stateManager.setStreamingText).toHaveBeenCalledWith('')
    expect(stateManager.finalizeMessage).toHaveBeenCalledWith('Hello world')
    expect(listener).toHaveBeenCalledWith({ type: 'streamingStop' })
  })

  it('appendStreaming auto-starts when not already streaming', () => {
    controller.appendStreaming('Hi')
    expect(listener).toHaveBeenCalledWith({ type: 'streamingStart' })
  })

  it('clear resets streaming state', () => {
    controller.startStreaming()
    controller.appendStreaming('x')
    controller.clear()
    expect(stateManager.setStreamingText).toHaveBeenCalledWith('')
  })
})
