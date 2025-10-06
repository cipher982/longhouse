#!/usr/bin/env node

/**
 * UI Switching Validation Test
 * Quick test to verify that the UI switching mechanism works correctly
 */

import { chromium } from 'playwright';

async function testUIType(useRustUI) {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  // Set the UI preference
  if (!useRustUI) {
    await page.addInitScript(() => {
      window.localStorage.setItem('zerg_use_react_dashboard', '1');
      window.localStorage.setItem('zerg_use_react_chat', '1');
    });
  }

  try {
    // Navigate to the app
    await page.goto('http://localhost:47200');

    // Check which UI loaded by looking for unique elements
    const isReactUI = await page.evaluate(() => {
      // React UI uses a specific root element
      return document.getElementById('react-root') !== null;
    });

    const isRustUI = await page.evaluate(() => {
      // Rust UI uses wasm-bindgen specific elements
      return document.querySelector('script[src*="bootstrap.js"]') !== null;
    });

    console.log(`UI Type: ${useRustUI ? 'Rust' : 'React'}`);
    console.log(`  React root found: ${isReactUI}`);
    console.log(`  Rust/WASM found: ${isRustUI}`);
    console.log(`  ✅ Correct UI loaded: ${useRustUI ? isRustUI : isReactUI}`);

    return {
      success: useRustUI ? isRustUI : isReactUI,
      details: { isReactUI, isRustUI }
    };
  } finally {
    await browser.close();
  }
}

async function main() {
  console.log('🔍 UI Switching Validation Test\n');
  console.log('Testing Rust UI...');
  const rustResult = await testUIType(true);

  console.log('\nTesting React UI...');
  const reactResult = await testUIType(false);

  console.log('\n📊 Results:');
  console.log('━━━━━━━━━━━━━━━━━━━━━━━━━━━━');
  console.log(`Rust UI loads correctly:  ${rustResult.success ? '✅' : '❌'}`);
  console.log(`React UI loads correctly: ${reactResult.success ? '✅' : '❌'}`);
  console.log('━━━━━━━━━━━━━━━━━━━━━━━━━━━━');

  if (rustResult.success && reactResult.success) {
    console.log('\n✅ UI switching mechanism is working correctly!');
    process.exit(0);
  } else {
    console.log('\n❌ UI switching has issues. Check the configuration.');
    process.exit(1);
  }
}

main().catch(console.error);