import { describe, expect, it, beforeEach } from 'vitest'
import { eventBus } from '../event-bus'
import { commisProgressStore } from '../commis-progress-store'

function resetStoreState() {
  // The store is a singleton; reset via public API events/state setters.
  commisProgressStore.setReconnecting(1)
}

describe('commisProgressStore', () => {
  beforeEach(() => {
    resetStoreState()
  })

  it('maps orphan commis (tool events) to real jobId on commis_complete', () => {
    const commisId = 'w-1'

    // Tool events can arrive before we have a jobId (e.g. after refresh).
    eventBus.emit('commis:tool_started', {
      commisId,
      toolName: 'ssh_exec',
      toolCallId: 'call-1',
      argsPreview: '{}',
      timestamp: Date.now(),
    })

    const stateAfterTool = commisProgressStore.getState()
    expect(stateAfterTool.commis.size).toBe(1)
    const orphanCommis = Array.from(stateAfterTool.commis.values())[0]
    expect(orphanCommis.commisId).toBe(commisId)
    expect(orphanCommis.status).toBe('running')
    expect(orphanCommis.jobId).toBeLessThan(0)

    // Later we receive the canonical jobId via concierge event.
    eventBus.emit('concierge:commis_complete', {
      jobId: 1,
      commisId,
      status: 'success',
      durationMs: 123,
      timestamp: Date.now(),
    })

    const state = commisProgressStore.getState()
    expect(state.commis.has(1)).toBe(true)
    const commis = state.commis.get(1)!
    expect(commis.commisId).toBe(commisId)
    expect(commis.status).toBe('complete')
  })
})
