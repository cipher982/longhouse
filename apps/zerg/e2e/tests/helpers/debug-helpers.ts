import { Page } from '@playwright/test';

/**
 * Enhanced debugging helper that captures browser console logs, errors, and network failures
 */
export async function setupDebugCapture(page: Page, testName: string) {
  const logs: string[] = [];
  const errors: string[] = [];
  const networkErrors: string[] = [];
  
  // Capture console logs
  page.on('console', (msg) => {
    const text = `[${msg.type()}] ${msg.text()}`;
    logs.push(text);
    
    // Log errors and warnings to console in real-time
    if (msg.type() === 'error' || msg.type() === 'warning') {
      console.log(`Browser ${msg.type()}: ${msg.text()}`);
    }
  });
  
  // Capture page errors
  page.on('pageerror', (error) => {
    const errorText = `Page error: ${error.message}\n${error.stack || ''}`;
    errors.push(errorText);
    console.error(errorText);
  });
  
  // Capture failed network requests
  page.on('requestfailed', (request) => {
    const failure = `Network request failed: ${request.method()} ${request.url()} - ${request.failure()?.errorText || 'Unknown error'}`;
    networkErrors.push(failure);
    console.error(failure);
  });
  
  // Return a function to dump all captured logs
  return {
    dumpLogs: () => {
      console.log(`\n=== Debug logs for test: ${testName} ===`);
      console.log('Console logs:', logs.length ? logs.join('\n') : 'No logs');
      console.log('\nErrors:', errors.length ? errors.join('\n') : 'No errors');
      console.log('\nNetwork errors:', networkErrors.length ? networkErrors.join('\n') : 'No network errors');
      console.log('=== End debug logs ===\n');
    },
    logs,
    errors,
    networkErrors
  };
}

/**
 * Wait for WASM to initialize by checking for specific indicators
 */
export async function waitForWasmInit(page: Page, timeout: number = 30000): Promise<boolean> {
  try {
    // Check if WASM module is loaded
    const wasmLoaded = await page.waitForFunction(
      () => {
        // Check for any signs that WASM has loaded
        // Look for window.__wasm_initialized or similar flags
        return (window as any).__wasm_initialized || 
               (document.querySelector('#app-container')?.children.length ?? 0) > 0 ||
               (document.querySelector('#dashboard-root')?.children.length ?? 0) > 0;
      },
      { timeout }
    );
    
    return !!wasmLoaded;
  } catch (error) {
    console.error('WASM initialization timeout:', error);
    return false;
  }
}

/**
 * Check what's actually rendered in the DOM
 */
export async function debugDomState(page: Page) {
  const domInfo = await page.evaluate(() => {
    const info: any = {
      title: document.title,
      bodyHTML: document.body.innerHTML.substring(0, 500),
      appContainer: document.querySelector('#app-container')?.innerHTML.substring(0, 200) || 'Not found',
      dashboardRoot: document.querySelector('#dashboard-root')?.innerHTML.substring(0, 200) || 'Not found',
      dashboardContainer: document.querySelector('#dashboard-container')?.innerHTML.substring(0, 200) || 'Not found',
      visibleElements: []
    };
    
    // Find all visible elements
    const allElements = document.querySelectorAll('*');
    allElements.forEach((el: any) => {
      if (el.offsetWidth > 0 && el.offsetHeight > 0 && el.id) {
        info.visibleElements.push({
          id: el.id,
          tagName: el.tagName,
          className: el.className
        });
      }
    });
    
    return info;
  });
  
  console.log('DOM State:', JSON.stringify(domInfo, null, 2));
  return domInfo;
}
