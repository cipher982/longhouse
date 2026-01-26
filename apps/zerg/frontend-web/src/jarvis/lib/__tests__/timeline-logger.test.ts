/**
 * Unit tests for TimelineLogger
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { TimelineLogger } from '../timeline-logger'
import { eventBus } from '../event-bus'

describe('TimelineLogger', () => {
  let logger: TimelineLogger
  let consoleGroupCollapsed: ReturnType<typeof vi.spyOn>
  let consoleLog: ReturnType<typeof vi.spyOn>
  let consoleGroupEnd: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    // Mock console methods
    consoleGroupCollapsed = vi.spyOn(console, 'groupCollapsed').mockImplementation(() => {})
    consoleLog = vi.spyOn(console, 'log').mockImplementation(() => {})
    consoleGroupEnd = vi.spyOn(console, 'groupEnd').mockImplementation(() => {})

    // Mock window.location.search to enable timeline
    // Use globalThis to ensure it works in test environment
    Object.defineProperty(globalThis, 'window', {
      value: {
        location: { search: '?timeline=true' },
      },
      writable: true,
      configurable: true,
    })

    logger = new TimelineLogger()
  })

  afterEach(() => {
    logger.dispose()
    consoleGroupCollapsed.mockRestore()
    consoleLog.mockRestore()
    consoleGroupEnd.mockRestore()
  })

  it('should be enabled when URL param timeline=true', () => {
    // Logger should have set up listeners
    expect((logger as any).enabled).toBe(true)
    expect((logger as any).unsubscribers.length).toBeGreaterThan(0)
  })

  it('should capture text_channel:sent event', () => {
    const messageId = 'test-123'
    logger.setMessageId(messageId)

    // Emit text_channel:sent event
    eventBus.emit('text_channel:sent', {
      text: 'Hello world',
      timestamp: Date.now(),
    })

    // Check that event was recorded
    const events = (logger as any).events as Array<any>
    expect(events.length).toBe(1)
    expect(events[0].phase).toBe('send')
  })

  it('should capture concierge lifecycle events', () => {
    const messageId = 'test-123'
    logger.setMessageId(messageId)

    const now = Date.now()

    // Send
    eventBus.emit('text_channel:sent', { text: 'test', timestamp: now })

    // Concierge started
    eventBus.emit('concierge:started', { courseId: 1, task: 'test task', timestamp: now + 100 })

    // Check events before complete (which triggers output and reset)
    let events = (logger as any).events as Array<any>
    expect(events.length).toBe(2)
    expect(events[0].phase).toBe('send')
    expect(events[1].phase).toBe('concierge_started')

    // Concierge complete (triggers output and reset)
    eventBus.emit('concierge:complete', {
      courseId: 1,
      result: 'Done',
      status: 'success',
      timestamp: now + 500,
    })

    // Events should be reset after output
    events = (logger as any).events as Array<any>
    expect(events.length).toBe(0)
  })

  it('should output timeline on concierge:complete', () => {
    const messageId = 'test-123'
    logger.setMessageId(messageId)

    const now = Date.now()

    // Emit events
    eventBus.emit('text_channel:sent', { text: 'test', timestamp: now })
    eventBus.emit('concierge:started', { courseId: 1, task: 'test', timestamp: now + 50 })
    eventBus.emit('concierge:complete', {
      courseId: 1,
      result: 'Done',
      status: 'success',
      timestamp: now + 200,
    })

    // Timeline should be output
    expect(consoleGroupCollapsed).toHaveBeenCalledWith(
      expect.stringContaining('[Timeline] test-123')
    )
    expect(consoleLog).toHaveBeenCalled()
    expect(consoleGroupEnd).toHaveBeenCalled()

    // Events should be reset
    const events = (logger as any).events as Array<any>
    expect(events.length).toBe(0)
  })

  it('should calculate T+offset from first event', () => {
    const messageId = 'test-123'
    logger.setMessageId(messageId)

    const now = Date.now()

    // Emit events with specific timestamps
    eventBus.emit('text_channel:sent', { text: 'test', timestamp: now })
    eventBus.emit('concierge:started', { courseId: 1, task: 'test', timestamp: now + 100 })
    eventBus.emit('concierge:complete', {
      courseId: 1,
      result: 'Done',
      status: 'success',
      timestamp: now + 300,
    })

    // Check console output includes T+ offsets
    // Find the timeline output (not the "Enabled" message)
    const timelineLogCall = consoleLog.mock.calls.find(
      (call) => typeof call[0] === 'string' && call[0].includes('T+')
    )
    expect(timelineLogCall).toBeDefined()
    const logOutput = timelineLogCall![0] as string
    expect(logOutput).toContain('T+0ms')
    expect(logOutput).toContain('T+100ms')
    expect(logOutput).toContain('T+300ms')
  })

  it('should capture commis and tool events', () => {
    const messageId = 'test-123'
    logger.setMessageId(messageId)

    const now = Date.now()

    // Emit events (but not complete yet)
    eventBus.emit('text_channel:sent', { text: 'test', timestamp: now })
    eventBus.emit('concierge:commis_spawned', { jobId: 1, task: 'commis task', timestamp: now + 50 })
    eventBus.emit('concierge:commis_started', { jobId: 1, commisId: 'commis-1', timestamp: now + 100 })
    eventBus.emit('commis:tool_started', {
      commisId: 'commis-1',
      toolName: 'ssh_exec',
      toolCallId: 'tool-1',
      timestamp: now + 150,
    })
    eventBus.emit('commis:tool_completed', {
      commisId: 'commis-1',
      toolName: 'ssh_exec',
      toolCallId: 'tool-1',
      durationMs: 200,
      timestamp: now + 350,
    })
    eventBus.emit('concierge:commis_complete', {
      jobId: 1,
      commisId: 'commis-1',
      status: 'success',
      durationMs: 400,
      timestamp: now + 500,
    })

    // Check events captured before complete
    let events = (logger as any).events as Array<any>
    expect(events.length).toBe(6)
    expect(events.map((e: any) => e.phase)).toEqual([
      'send',
      'commis_spawned',
      'commis_started',
      'tool_started',
      'tool_completed',
      'commis_complete',
    ])

    // Emit complete (triggers output and reset)
    eventBus.emit('concierge:complete', {
      courseId: 1,
      result: 'Done',
      status: 'success',
      timestamp: now + 600,
    })

    // Events should be reset after output
    events = (logger as any).events as Array<any>
    expect(events.length).toBe(0)
  })

  it('should not track events when timeline disabled', () => {
    // Reset window.location to disable timeline
    Object.defineProperty(globalThis, 'window', {
      value: {
        location: { search: '' },
      },
      writable: true,
      configurable: true,
    })

    const disabledLogger = new TimelineLogger()

    // Emit event
    eventBus.emit('text_channel:sent', { text: 'test', timestamp: Date.now() })

    // No events should be captured
    const events = (disabledLogger as any).events as Array<any>
    expect(events.length).toBe(0)

    disabledLogger.dispose()
  })
})
