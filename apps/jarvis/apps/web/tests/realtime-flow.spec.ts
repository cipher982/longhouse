import { describe, it, expect, beforeEach } from 'vitest'
import { ConversationRenderer } from '../lib/conversation-renderer'

/**
 * Simulate a realtime flow:
 * 1) VAD speech started -> pending user placeholder (early timestamp)
 * 2) Transcription deltas (ignored here; renderer only cares about final update)
 * 3) Final transcript updates the same pending message
 * 4) Assistant streaming starts, updates, and finalizes
 * Verify chronological order and that streaming remains a single message.
 */

describe('Realtime conversation flow', () => {
  let el: HTMLDivElement
  let renderer: ConversationRenderer

  beforeEach(() => {
    el = document.createElement('div')
    document.body.innerHTML = ''
    document.body.appendChild(el)
    renderer = new ConversationRenderer(el)
  })

  it('keeps turns ordered and streams in one message', () => {
    const tUserStart = new Date('2024-01-01T10:00:00.000Z')
    const tAssistantStart = new Date('2024-01-01T10:00:01.000Z')

    // 1) Pending user placeholder at speech start
    const pendingId = 'pending-user-1'
    renderer.addMessage({ id: pendingId, role: 'user', content: 'Listening…', timestamp: tUserStart })

    // 2) Final user transcript arrives: update same message (keeps tUserStart)
    renderer.updateMessage(pendingId, { content: 'What tools can you call?' })

    // 3) Assistant streaming starts
    const streamId = 'stream-1'
    renderer.addMessage({ id: streamId, role: 'assistant', content: '', timestamp: tAssistantStart, isStreaming: true })
    renderer.updateMessage(streamId, { content: 'I have access to several tool' })
    renderer.updateMessage(streamId, { content: 'I have access to several tools.' })
    renderer.updateMessage(streamId, { isStreaming: false })

    const html = el.innerHTML
    const userIdx = html.indexOf('What tools can you call?')
    const asstIdx = html.indexOf('I have access to several tools.')
    expect(userIdx).toBeGreaterThanOrEqual(0)
    expect(asstIdx).toBeGreaterThanOrEqual(0)
    expect(userIdx).toBeLessThan(asstIdx)

    // Ensure there is only one assistant message rendered
    const assistantTurns = el.querySelectorAll('.assistant-turn')
    expect(assistantTurns.length).toBe(1)
  })
})
