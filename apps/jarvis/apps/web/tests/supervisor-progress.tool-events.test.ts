import { describe, it, expect, beforeEach, afterEach } from 'vitest';

import { WorkerProgressUI } from '../lib/supervisor-progress';
import { eventBus } from '../lib/event-bus';

describe('WorkerProgressUI tool events', () => {
  let ui: WorkerProgressUI;

  beforeEach(() => {
    document.body.innerHTML = '<div id="supervisor-progress"></div>';
    ui = new WorkerProgressUI();
    ui.initialize('supervisor-progress');
    eventBus.emit('supervisor:started', { runId: 1, task: 'test task', timestamp: Date.now() });
    // Progress UI only renders once workers spawn (isActive becomes true)
    eventBus.emit('supervisor:worker_spawned', { jobId: 1, task: 'worker task', timestamp: Date.now() });
    eventBus.emit('supervisor:worker_started', { jobId: 1, workerId: 'worker-1', timestamp: Date.now() });
  });

  afterEach(() => {
    ui.destroy();
    document.body.innerHTML = '';
  });

  it('uses tool name when completion arrives before started', () => {
    eventBus.emit('worker:tool_completed', {
      workerId: 'worker-1',
      toolName: 'ssh_exec',
      toolCallId: 'call-1',
      durationMs: 500,
      resultPreview: 'ok',
      timestamp: Date.now(),
    });

    const workers = (ui as unknown as { workers: Map<number, any> }).workers;
    const worker = Array.from(workers.values())[0];
    const tool = worker.toolCalls.get('call-1');

    expect(tool.toolName).toBe('ssh_exec');

    const html = document.getElementById('supervisor-progress')?.innerHTML || '';
    expect(html).toContain('ssh_exec');
  });

  it('drops tool events with empty workerId', () => {
    const workersBefore = (ui as unknown as { workers: Map<number, any> }).workers.size;
    eventBus.emit('worker:tool_started', {
      workerId: '',
      toolName: 'ssh_exec',
      toolCallId: 'call-1',
      argsPreview: 'noop',
      timestamp: Date.now(),
    });

    const workers = (ui as unknown as { workers: Map<number, any> }).workers;
    expect(workers.size).toBe(workersBefore);
  });
});
