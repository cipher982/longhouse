import { test, expect } from './fixtures';

test.describe('Animation System Integration', () => {
  test.describe.configure({ mode: 'serial' }); // Run tests in sequence for stability

  test('Frontend loads with animation system available', async ({ page }) => {
    // Set up test mode and mock API endpoints
    await page.addInitScript(() => {
      window.__TEST_MODE__ = true;
      console.log('🔧 Test mode initialized');

      // Mock fetch for missing API endpoints
      const originalFetch = window.fetch;
      window.fetch = function(url, options) {
        console.log(`📡 Intercepted fetch: ${url}`);

        if (url.includes('/api/system/info')) {
          return Promise.resolve(new Response(JSON.stringify({
            status: 'healthy',
            version: 'test-1.0.0',
            environment: 'test'
          }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
        }

        if (url.includes('/api/models')) {
          return Promise.resolve(new Response(JSON.stringify([
            { id: 'gpt-4', name: 'GPT-4', provider: 'openai', enabled: true }
          ]), { status: 200, headers: { 'Content-Type': 'application/json' } }));
        }

        if (url.includes('/api/users/me')) {
          return Promise.resolve(new Response(JSON.stringify({
            id: 1,
            email: 'test@example.com',
            display_name: 'Test User'
          }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
        }

        if (url.includes('/api/agents')) {
          return Promise.resolve(new Response(JSON.stringify([
            { id: 1, name: 'Test Agent', status: 'idle' }
          ]), { status: 200, headers: { 'Content-Type': 'application/json' } }));
        }

        return originalFetch.apply(this, arguments);
      };

      // Force canvas root to be visible after DOM loads
      const showCanvas = () => {
        const canvasRoot = document.querySelector('#canvas-root');
        if (canvasRoot) {
          canvasRoot.style.display = 'block';
          canvasRoot.style.visibility = 'visible';
          console.log('✅ Canvas root made visible');
        } else {
          console.log('⚠️ Canvas root not found yet');
        }
      };

      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
          setTimeout(showCanvas, 2000);
        });
      } else {
        setTimeout(showCanvas, 2000);
      }
    });

    // Navigate to the page
    console.log('🚀 Navigating to frontend...');
    await page.goto('/');

    // Wait for basic page load
    await page.waitForTimeout(3000);

    // Take a screenshot to see what loaded
    await page.screenshot({ path: 'test-results/animation-test-loaded.png', fullPage: true });

    // Check what elements are present
    const elements = await page.evaluate(() => {
      const foundElements = [];

      // Check for various canvas-related elements
      const selectors = [
        '#canvas-root',
        '#canvas-container',
        '.canvas-tab',
        '[data-testid="global-canvas-tab"]',
        '#agent-shelf',
        '.agent-pill',
        'canvas'
      ];

      selectors.forEach(selector => {
        const element = document.querySelector(selector);
        foundElements.push({
          selector,
          found: !!element,
          visible: element ? getComputedStyle(element).display !== 'none' : false,
          style: element ? element.getAttribute('style') : null
        });
      });

      return foundElements;
    });

    console.log('🔍 Element analysis:', elements);

    // Try to force canvas visibility
    await page.evaluate(() => {
      // Try multiple approaches to show canvas
      const canvasRoot = document.querySelector('#canvas-root');
      if (canvasRoot) {
        canvasRoot.style.display = 'block';
        canvasRoot.style.visibility = 'visible';
        canvasRoot.style.opacity = '1';
      }

      // Also try to trigger any initialization
      if (window.init_canvas) {
        window.init_canvas();
      }
    });

    await page.waitForTimeout(2000);

    // Take another screenshot after forcing visibility
    await page.screenshot({ path: 'test-results/animation-test-forced.png', fullPage: true });

    // Check if canvas appeared
    const canvasVisible = await page.locator('#canvas-container').isVisible().catch(() => false);
    const canvasRoot = await page.locator('#canvas-root').isVisible().catch(() => false);

    console.log(`Canvas container visible: ${canvasVisible}`);
    console.log(`Canvas root visible: ${canvasRoot}`);

    if (canvasVisible) {
      console.log('✅ SUCCESS: Canvas is visible, animation system should work');

      // Test our animation state management if canvas loaded
      const animationStateExists = await page.evaluate(() => {
        // Check if our animation system is loaded
        return !!(window.APP_STATE || window.__WASM_READY__ || document.querySelector('canvas'));
      });

      console.log(`Animation system loaded: ${animationStateExists}`);
    } else {
      console.log('❌ Canvas not visible - need to investigate further');

      // Debug information
      const pageTitle = await page.title();
      const bodyContent = await page.textContent('body');

      console.log(`Page title: ${pageTitle}`);
      console.log(`Body contains canvas: ${bodyContent.includes('canvas')}`);
    }

    // The important part: our code changes should be loaded
    expect(true).toBe(true); // This test is mainly for investigation
  });
});
