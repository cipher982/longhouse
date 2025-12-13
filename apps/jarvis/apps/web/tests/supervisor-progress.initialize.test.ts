import { describe, it, expect, afterEach } from 'vitest';

import { SupervisorProgressUI } from '../lib/supervisor-progress';

describe('SupervisorProgressUI initialize', () => {
  afterEach(() => {
    document.body.innerHTML = '';
  });

  it('normalizes a pre-rendered placeholder in floating mode', () => {
    document.body.innerHTML = `
      <div class="app-root">
        <div id="supervisor-progress" class="hidden supervisor-progress-panel"></div>
      </div>
    `;

    const ui = new SupervisorProgressUI();
    ui.initialize('supervisor-progress', 'floating');

    const el = document.getElementById('supervisor-progress');
    expect(el).toBeTruthy();
    expect(el?.classList.contains('hidden')).toBe(false);
    expect(el?.classList.contains('supervisor-progress')).toBe(true);
    expect(el?.classList.contains('supervisor-progress--floating')).toBe(true);
    expect(el?.parentElement).toBe(document.body);

    ui.destroy();
  });
});
