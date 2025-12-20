import { describe, it, expect, afterEach } from 'vitest';

import { WorkerProgressUI } from '../lib/worker-progress';

describe('WorkerProgressUI initialize', () => {
  afterEach(() => {
    document.body.innerHTML = '';
  });

  it('normalizes a pre-rendered placeholder in floating mode', () => {
    document.body.innerHTML = `
      <div class="app-root">
        <div id="worker-progress" class="hidden worker-progress-panel"></div>
      </div>
    `;

    const ui = new WorkerProgressUI();
    ui.initialize('worker-progress', 'floating');

    const el = document.getElementById('worker-progress');
    expect(el).toBeTruthy();
    expect(el?.classList.contains('hidden')).toBe(false);
    expect(el?.classList.contains('worker-progress')).toBe(true);
    expect(el?.classList.contains('worker-progress--floating')).toBe(true);
    expect(el?.parentElement).toBe(document.body);

    ui.destroy();
  });
});
