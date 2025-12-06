import { describe, it, expect, beforeEach } from 'vitest'
import { ConversationRenderer } from '../lib/conversation-renderer'

describe('ConversationRenderer', () => {
  let el: HTMLDivElement
  let renderer: ConversationRenderer

  beforeEach(() => {
    el = document.createElement('div')
    document.body.innerHTML = ''
    document.body.appendChild(el)
    renderer = new ConversationRenderer(el)
  })

  it('renders messages in chronological order regardless of add order', () => {
    const t1 = new Date('2024-01-01T10:00:00Z')
    const t2 = new Date('2024-01-01T10:00:01Z')
    const t3 = new Date('2024-01-01T10:00:02Z')

    renderer.addMessage({ id: '2', role: 'assistant', content: 'Hello there!', timestamp: t2 })
    renderer.addMessage({ id: '1', role: 'user', content: 'Hello', timestamp: t1 })
    renderer.addMessage({ id: '3', role: 'user', content: 'How are you?', timestamp: t3 })

    const html = el.innerHTML
    const idx1 = html.indexOf('Hello')
    const idx2 = html.indexOf('Hello there!')
    const idx3 = html.indexOf('How are you?')

    expect(idx1).toBeGreaterThanOrEqual(0)
    expect(idx2).toBeGreaterThanOrEqual(0)
    expect(idx3).toBeGreaterThanOrEqual(0)
    expect(idx1).toBeLessThan(idx2)
    expect(idx2).toBeLessThan(idx3)
  })

  it('shows status when empty, clears when messages appear', () => {
    renderer.setStatus('Ready to connect', true)
    expect(el.innerHTML).toContain('Ready to connect')
    expect(el.querySelector('.muted')).toBeTruthy()

    const t1 = new Date('2024-01-01T10:00:00Z')
    renderer.addMessage({ id: '1', role: 'user', content: 'Hi', timestamp: t1 })
    expect(el.innerHTML).not.toContain('Ready to connect')
    expect(el.querySelector('.user-turn')).toBeTruthy()
  })

  it('keeps a single streaming message updated in place', () => {
    const t = new Date('2024-01-01T10:00:00Z')
    const id = 'stream-1'
    renderer.addMessage({ id, role: 'assistant', content: '', timestamp: t, isStreaming: true })

    renderer.updateMessage(id, { content: 'Thi' })
    renderer.updateMessage(id, { content: 'Thinking...' })
    let html = el.innerHTML
    const occurrences = (html.match(/Thinking\.\.\./g) || []).length
    expect(occurrences).toBe(1)
    expect(html).toContain('cursor')

    renderer.updateMessage(id, { isStreaming: false })
    html = el.innerHTML
    expect(html).not.toContain('cursor')
  })

  it('uses seq tie-breaker for identical timestamps (deterministic)', () => {
    const t = new Date('2024-01-01T10:00:00Z')
    renderer.addMessage({ id: 'a', role: 'user', content: 'First', timestamp: t })
    renderer.addMessage({ id: 'b', role: 'assistant', content: 'Second', timestamp: t })
    const html = el.innerHTML
    expect(html.indexOf('First')).toBeLessThan(html.indexOf('Second'))
  })
})
