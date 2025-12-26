import { describe, expect, it, beforeEach } from 'vitest'
import { eventBus } from '../event-bus'
import { workerProgressStore } from '../worker-progress-store'

function resetStoreState() {
  // The store is a singleton; reset via public API events/state setters.
  workerProgressStore.setReconnecting(1)
}

describe('workerProgressStore', () => {
  beforeEach(() => {
    resetStoreState()
  })

  it('maps orphan worker (tool events) to real jobId on worker_complete', () => {
    const workerId = 'w-1'

    // Tool events can arrive before we have a jobId (e.g. after refresh).
    eventBus.emit('worker:tool_started', {
      workerId,
      toolName: 'ssh_exec',
      toolCallId: 'call-1',
      argsPreview: '{}',
      timestamp: Date.now(),
    })

    const stateAfterTool = workerProgressStore.getState()
    expect(stateAfterTool.workers.size).toBe(1)
    const orphanWorker = Array.from(stateAfterTool.workers.values())[0]
    expect(orphanWorker.workerId).toBe(workerId)
    expect(orphanWorker.status).toBe('running')
    expect(orphanWorker.jobId).toBeLessThan(0)

    // Later we receive the canonical jobId via supervisor event.
    eventBus.emit('supervisor:worker_complete', {
      jobId: 1,
      workerId,
      status: 'success',
      durationMs: 123,
      timestamp: Date.now(),
    })

    const state = workerProgressStore.getState()
    expect(state.workers.has(1)).toBe(true)
    const worker = state.workers.get(1)!
    expect(worker.workerId).toBe(workerId)
    expect(worker.status).toBe('complete')
  })
})
