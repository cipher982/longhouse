/**
 * Performance Debug Script for Jarvis CSS Effects
 *
 * Usage: Open browser console and run:
 *   const perf = await import('/debug-perf.js'); perf.init();
 *
 * Or paste this entire script into the console.
 */

const effects = {
  nebulaBlur: {
    name: 'Nebula Blur (60px filter)',
    selector: '.jarvis-container .app-container::after',
    property: 'filter',
    enabled: true,
    // Can't directly toggle ::after, so we use a class approach
    toggle: (on) => {
      document.querySelector('.jarvis-container .app-container')
        ?.classList.toggle('no-nebula', !on);
    }
  },
  nebulaAnimation: {
    name: 'Nebula Animation (30s drift)',
    selector: '.jarvis-container .app-container::after',
    property: 'animation',
    enabled: true,
    toggle: (on) => {
      document.querySelector('.jarvis-container .app-container')
        ?.classList.toggle('no-nebula-animation', !on);
    }
  },
  gridAnimation: {
    name: 'Grid Animation (20s move + perspective)',
    selector: '.jarvis-container .app-container::before',
    property: 'animation',
    enabled: true,
    toggle: (on) => {
      document.querySelector('.jarvis-container .app-container')
        ?.classList.toggle('no-grid', !on);
    }
  },
  backdropBlur: {
    name: 'Main Content Backdrop Blur',
    selector: '.jarvis-container .main-content',
    property: 'backdrop-filter',
    enabled: true,
    toggle: (on) => {
      const el = document.querySelector('.jarvis-container .main-content');
      if (el) el.style.backdropFilter = on ? '' : 'none';
    }
  },
  logoPulse: {
    name: 'Logo Pulse Animation',
    selector: '.brand-logo-glow',
    property: 'animation',
    enabled: true,
    toggle: (on) => {
      const el = document.querySelector('.brand-logo-glow');
      if (el) el.style.animationPlayState = on ? 'running' : 'paused';
    }
  }
};

// Inject CSS overrides for pseudo-element toggles
function injectToggleStyles() {
  if (document.getElementById('perf-debug-styles')) return;

  const style = document.createElement('style');
  style.id = 'perf-debug-styles';
  style.textContent = `
    /* Toggle classes for pseudo-elements */
    .jarvis-container .app-container.no-nebula::after {
      filter: none !important;
      background: none !important;
    }
    .jarvis-container .app-container.no-nebula-animation::after {
      animation: none !important;
    }
    .jarvis-container .app-container.no-grid::before {
      animation: none !important;
      background: none !important;
    }
  `;
  document.head.appendChild(style);
}

// FPS Monitor
let fpsData = [];
let fpsInterval = null;
let lastFrameTime = performance.now();
let frameCount = 0;

function startFPSMonitor() {
  fpsData = [];
  frameCount = 0;
  lastFrameTime = performance.now();

  function measureFrame() {
    frameCount++;
    const now = performance.now();
    const delta = now - lastFrameTime;

    if (delta >= 1000) {
      const fps = Math.round((frameCount * 1000) / delta);
      fpsData.push(fps);
      frameCount = 0;
      lastFrameTime = now;
    }

    if (fpsInterval !== null) {
      requestAnimationFrame(measureFrame);
    }
  }

  fpsInterval = true;
  requestAnimationFrame(measureFrame);
}

function stopFPSMonitor() {
  fpsInterval = null;
  return {
    samples: fpsData.length,
    avg: fpsData.length ? Math.round(fpsData.reduce((a, b) => a + b, 0) / fpsData.length) : 0,
    min: fpsData.length ? Math.min(...fpsData) : 0,
    max: fpsData.length ? Math.max(...fpsData) : 0,
    data: [...fpsData]
  };
}

// Run A/B test on a single effect
async function testEffect(effectKey, durationMs = 5000) {
  const effect = effects[effectKey];
  if (!effect) {
    console.error(`Unknown effect: ${effectKey}`);
    return;
  }

  console.log(`\n🧪 Testing: ${effect.name}`);
  console.log(`   Duration: ${durationMs/1000}s per state\n`);

  // Test with effect ON
  effect.toggle(true);
  console.log('   📊 Measuring with effect ON...');
  startFPSMonitor();
  await sleep(durationMs);
  const onStats = stopFPSMonitor();

  // Test with effect OFF
  effect.toggle(false);
  console.log('   📊 Measuring with effect OFF...');
  startFPSMonitor();
  await sleep(durationMs);
  const offStats = stopFPSMonitor();

  // Restore original state
  effect.toggle(effect.enabled);

  // Report
  const improvement = onStats.avg > 0 ? Math.round((offStats.avg - onStats.avg) / onStats.avg * 100) : 0;

  console.log(`\n   ✅ Results for "${effect.name}":`);
  console.log(`   ┌────────────────┬────────┬────────┐`);
  console.log(`   │                │   ON   │   OFF  │`);
  console.log(`   ├────────────────┼────────┼────────┤`);
  console.log(`   │ Avg FPS        │ ${String(onStats.avg).padStart(6)} │ ${String(offStats.avg).padStart(6)} │`);
  console.log(`   │ Min FPS        │ ${String(onStats.min).padStart(6)} │ ${String(offStats.min).padStart(6)} │`);
  console.log(`   │ Max FPS        │ ${String(onStats.max).padStart(6)} │ ${String(offStats.max).padStart(6)} │`);
  console.log(`   └────────────────┴────────┴────────┘`);
  console.log(`   ${improvement > 0 ? '🚀' : '➡️'} FPS change when disabled: ${improvement > 0 ? '+' : ''}${improvement}%\n`);

  return { effectKey, name: effect.name, on: onStats, off: offStats, improvement };
}

// Run full suite
async function testAll(durationMs = 3000) {
  console.log('╔═══════════════════════════════════════════════════════════╗');
  console.log('║     JARVIS CSS PERFORMANCE PROFILER                       ║');
  console.log('║     Testing each effect individually                      ║');
  console.log('╚═══════════════════════════════════════════════════════════╝');

  const results = [];

  for (const key of Object.keys(effects)) {
    const result = await testEffect(key, durationMs);
    results.push(result);
  }

  // Summary
  console.log('\n═══════════════════════════════════════════════════════════');
  console.log('                     SUMMARY');
  console.log('═══════════════════════════════════════════════════════════');

  results.sort((a, b) => b.improvement - a.improvement);

  for (const r of results) {
    const bar = '█'.repeat(Math.min(20, Math.max(0, Math.round(r.improvement / 5))));
    const icon = r.improvement > 10 ? '🔴' : r.improvement > 5 ? '🟡' : '🟢';
    console.log(`${icon} ${r.name}`);
    console.log(`   ${bar} +${r.improvement}% FPS when disabled`);
  }

  console.log('\n💡 Recommendation: Disable effects with 🔴 for best performance');

  return results;
}

// Interactive toggle UI
function showUI() {
  // Remove existing UI
  document.getElementById('perf-debug-ui')?.remove();

  const ui = document.createElement('div');
  ui.id = 'perf-debug-ui';
  ui.innerHTML = `
    <style>
      #perf-debug-ui {
        position: fixed;
        top: 10px;
        right: 10px;
        background: rgba(0,0,0,0.9);
        color: #fff;
        padding: 16px;
        border-radius: 8px;
        font-family: monospace;
        font-size: 12px;
        z-index: 99999;
        min-width: 280px;
        box-shadow: 0 4px 20px rgba(0,0,0,0.5);
      }
      #perf-debug-ui h3 {
        margin: 0 0 12px 0;
        font-size: 14px;
        color: #6366f1;
      }
      #perf-debug-ui label {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 0;
        cursor: pointer;
      }
      #perf-debug-ui label:hover {
        background: rgba(255,255,255,0.1);
      }
      #perf-debug-ui input[type="checkbox"] {
        width: 16px;
        height: 16px;
      }
      #perf-debug-ui .fps {
        color: #22c55e;
        font-size: 18px;
        font-weight: bold;
        margin: 12px 0;
      }
      #perf-debug-ui button {
        background: #6366f1;
        color: white;
        border: none;
        padding: 8px 16px;
        border-radius: 4px;
        cursor: pointer;
        margin-top: 8px;
        width: 100%;
      }
      #perf-debug-ui button:hover {
        background: #4f46e5;
      }
      #perf-debug-ui .close {
        position: absolute;
        top: 8px;
        right: 8px;
        background: none;
        border: none;
        color: #666;
        cursor: pointer;
        font-size: 16px;
        width: auto;
        padding: 4px 8px;
        margin: 0;
      }
    </style>
    <button class="close" onclick="document.getElementById('perf-debug-ui').remove()">✕</button>
    <h3>🔧 CSS Effect Profiler</h3>
    <div class="fps">FPS: <span id="perf-fps">--</span></div>
    <div id="perf-toggles"></div>
    <button onclick="window.__perfDebug.testAll()">Run Full Benchmark</button>
  `;

  document.body.appendChild(ui);

  // Add toggles
  const togglesContainer = document.getElementById('perf-toggles');
  for (const [key, effect] of Object.entries(effects)) {
    const label = document.createElement('label');
    label.innerHTML = `
      <input type="checkbox" ${effect.enabled ? 'checked' : ''} data-effect="${key}">
      ${effect.name}
    `;
    label.querySelector('input').addEventListener('change', (e) => {
      effect.enabled = e.target.checked;
      effect.toggle(e.target.checked);
    });
    togglesContainer.appendChild(label);
  }

  // Live FPS counter
  let lastTime = performance.now();
  let frames = 0;
  function updateFPS() {
    frames++;
    const now = performance.now();
    if (now - lastTime >= 500) {
      const fps = Math.round((frames * 1000) / (now - lastTime));
      document.getElementById('perf-fps').textContent = fps;
      frames = 0;
      lastTime = now;
    }
    requestAnimationFrame(updateFPS);
  }
  updateFPS();
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// Initialize
function init() {
  injectToggleStyles();
  showUI();

  // Expose to window for console access
  window.__perfDebug = {
    effects,
    testEffect,
    testAll,
    showUI,
    hideUI: () => document.getElementById('perf-debug-ui')?.remove()
  };

  console.log('🔧 Perf Debug initialized!');
  console.log('   Commands:');
  console.log('   - __perfDebug.testAll()     Run full benchmark');
  console.log('   - __perfDebug.testEffect("nebulaBlur")  Test single effect');
  console.log('   - __perfDebug.showUI()      Show toggle panel');
  console.log('   - __perfDebug.hideUI()      Hide toggle panel');
}

// Auto-init if loaded as module
if (typeof window !== 'undefined') {
  init();
}

export { init, effects, testEffect, testAll, showUI };
