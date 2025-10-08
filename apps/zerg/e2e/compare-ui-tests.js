#!/usr/bin/env node

/**
 * UI Comparison Test Runner
 * Runs E2E tests against both Rust/WASM and React UIs and compares results
 */

import { spawn } from 'child_process';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// ANSI color codes for terminal output
const colors = {
  reset: '\x1b[0m',
  bright: '\x1b[1m',
  dim: '\x1b[2m',
  red: '\x1b[31m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  blue: '\x1b[34m',
  cyan: '\x1b[36m',
  white: '\x1b[37m'
};

// Test scenarios to compare
const TEST_SCENARIOS = [
  {
    name: 'Agent Creation',
    file: 'agent_creation_full.spec.ts',
    description: 'Create agent via UI and API'
  },
  {
    name: 'Chat Functionality',
    file: 'chat_functional.spec.ts',
    description: 'Send messages and verify chat UI'
  },
  {
    name: 'Dashboard Operations',
    file: 'dashboard.parity.spec.ts',
    description: 'Dashboard UI parity and operations'
  },
  {
    name: 'Thread Management',
    file: 'chat_functional.spec.ts',
    testName: 'Thread creation and switching',
    description: 'Create and switch between threads'
  },
  {
    name: 'Real-time Updates',
    file: 'realtime_updates.spec.ts',
    description: 'WebSocket real-time features'
  }
];

// Results storage
const results = {
  rust: {},
  react: {}
};

/**
 * Run a single test file against a specific UI
 */
async function runTest(uiType, testFile, testName = null) {
  return new Promise((resolve) => {
    const env = {
      ...process.env,
      PLAYWRIGHT_USE_RUST_UI: uiType === 'rust' ? '1' : '0',
      CI: 'true',
      HEADLESS: 'true'
    };

    const args = [
      'test',
      testFile,
      '--reporter=json',
      '--quiet'
    ];

    if (testName) {
      args.push('-g', testName);
    }

    const proc = spawn('npx', ['playwright', ...args], {
      cwd: path.join(__dirname),
      env,
      stdio: ['ignore', 'pipe', 'pipe']
    });

    let output = '';
    let errorOutput = '';

    proc.stdout.on('data', (data) => {
      output += data.toString();
    });

    proc.stderr.on('data', (data) => {
      errorOutput += data.toString();
    });

    proc.on('close', (code) => {
      try {
        // Try to parse JSON output
        const jsonStart = output.indexOf('{');
        if (jsonStart !== -1) {
          const jsonStr = output.substring(jsonStart);
          const testResults = JSON.parse(jsonStr);

          const passed = testResults.suites?.every(suite =>
            suite.specs?.every(spec =>
              spec.tests?.every(test => test.status === 'passed')
            )
          ) ?? false;

          const stats = {
            passed,
            total: testResults.stats?.expected || 0,
            failed: testResults.stats?.unexpected || 0,
            duration: testResults.stats?.duration || 0
          };

          resolve({
            success: passed,
            stats,
            error: null
          });
        } else {
          // Fallback: check exit code
          resolve({
            success: code === 0,
            stats: { passed: code === 0, total: 1, failed: code === 0 ? 0 : 1 },
            error: code !== 0 ? errorOutput || 'Test failed' : null
          });
        }
      } catch (e) {
        // If JSON parsing fails, use exit code
        resolve({
          success: code === 0,
          stats: { passed: code === 0, total: 1, failed: code === 0 ? 0 : 1 },
          error: e.message
        });
      }
    });
  });
}

/**
 * Format duration for display
 */
function formatDuration(ms) {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/**
 * Print results table
 */
function printResultsTable() {
  console.log('\n' + colors.bright + colors.cyan + '=' .repeat(80) + colors.reset);
  console.log(colors.bright + colors.white + '                    E2E TEST RESULTS COMPARISON' + colors.reset);
  console.log(colors.bright + colors.cyan + '=' .repeat(80) + colors.reset);

  // Header
  console.log(
    colors.bright +
    '┌' + '─'.repeat(30) + '┬' + '─'.repeat(24) + '┬' + '─'.repeat(24) + '┐' +
    colors.reset
  );
  console.log(
    colors.bright +
    '│ ' + 'Test Scenario'.padEnd(29) +
    '│ ' + 'Rust/WASM UI'.padEnd(23) +
    '│ ' + 'React UI'.padEnd(23) +
    '│' +
    colors.reset
  );
  console.log(
    colors.bright +
    '├' + '─'.repeat(30) + '┼' + '─'.repeat(24) + '┼' + '─'.repeat(24) + '┤' +
    colors.reset
  );

  // Results rows
  TEST_SCENARIOS.forEach(scenario => {
    const rustResult = results.rust[scenario.name];
    const reactResult = results.react[scenario.name];

    const rustStatus = rustResult
      ? (rustResult.success
          ? colors.green + '✅ PASS' + colors.reset
          : colors.red + '❌ FAIL' + colors.reset)
      : colors.yellow + '⏭️  SKIP' + colors.reset;

    const reactStatus = reactResult
      ? (reactResult.success
          ? colors.green + '✅ PASS' + colors.reset
          : colors.red + '❌ FAIL' + colors.reset)
      : colors.yellow + '⏭️  SKIP' + colors.reset;

    const rustTime = rustResult?.stats?.duration
      ? colors.dim + ` (${formatDuration(rustResult.stats.duration)})` + colors.reset
      : '';

    const reactTime = reactResult?.stats?.duration
      ? colors.dim + ` (${formatDuration(reactResult.stats.duration)})` + colors.reset
      : '';

    console.log(
      '│ ' + scenario.name.padEnd(29) +
      '│ ' + (rustStatus + rustTime).padEnd(23 + 10) +  // Extra padding for color codes
      '│ ' + (reactStatus + reactTime).padEnd(23 + 10) +
      '│'
    );
  });

  console.log(
    colors.bright +
    '└' + '─'.repeat(30) + '┴' + '─'.repeat(24) + '┴' + '─'.repeat(24) + '┘' +
    colors.reset
  );

  // Summary statistics
  const rustStats = {
    passed: Object.values(results.rust).filter(r => r?.success).length,
    failed: Object.values(results.rust).filter(r => r && !r.success).length,
    skipped: TEST_SCENARIOS.length - Object.keys(results.rust).length
  };

  const reactStats = {
    passed: Object.values(results.react).filter(r => r?.success).length,
    failed: Object.values(results.react).filter(r => r && !r.success).length,
    skipped: TEST_SCENARIOS.length - Object.keys(results.react).length
  };

  console.log('\n' + colors.bright + colors.blue + 'Summary:' + colors.reset);
  console.log('├─ Rust/WASM: ' +
    colors.green + rustStats.passed + ' passed' + colors.reset + ', ' +
    colors.red + rustStats.failed + ' failed' + colors.reset + ', ' +
    colors.yellow + rustStats.skipped + ' skipped' + colors.reset
  );
  console.log('└─ React:     ' +
    colors.green + reactStats.passed + ' passed' + colors.reset + ', ' +
    colors.red + reactStats.failed + ' failed' + colors.reset + ', ' +
    colors.yellow + reactStats.skipped + ' skipped' + colors.reset
  );

  // Parity score
  const matchingResults = TEST_SCENARIOS.filter(scenario => {
    const rust = results.rust[scenario.name];
    const react = results.react[scenario.name];
    return rust?.success === react?.success && rust && react;
  }).length;

  const testedScenarios = TEST_SCENARIOS.filter(scenario =>
    results.rust[scenario.name] && results.react[scenario.name]
  ).length;

  if (testedScenarios > 0) {
    const parityPercentage = Math.round((matchingResults / testedScenarios) * 100);
    console.log('\n' + colors.bright + colors.cyan +
      `🎯 UI Parity Score: ${parityPercentage}% (${matchingResults}/${testedScenarios} matching results)` +
      colors.reset
    );
  }

  // Failed test details
  const failures = [];
  TEST_SCENARIOS.forEach(scenario => {
    if (results.rust[scenario.name]?.error) {
      failures.push(`  • ${scenario.name} (Rust): ${results.rust[scenario.name].error}`);
    }
    if (results.react[scenario.name]?.error) {
      failures.push(`  • ${scenario.name} (React): ${results.react[scenario.name].error}`);
    }
  });

  if (failures.length > 0) {
    console.log('\n' + colors.red + colors.bright + 'Failed Test Details:' + colors.reset);
    failures.forEach(f => console.log(colors.red + f + colors.reset));
  }

  console.log('\n' + colors.bright + colors.cyan + '=' .repeat(80) + colors.reset + '\n');
}

/**
 * Main execution
 */
async function main() {
  console.log(colors.bright + colors.blue + '\n🚀 Starting UI Comparison Tests...\n' + colors.reset);

  // Check if we're in the e2e directory
  if (!fs.existsSync('playwright.config.js')) {
    console.error(colors.red + '❌ Error: Must run from the e2e directory' + colors.reset);
    process.exit(1);
  }

  // Install dependencies if needed
  console.log(colors.dim + '📦 Ensuring dependencies are installed...' + colors.reset);
  await new Promise((resolve) => {
    spawn('npm', ['install'], { stdio: 'inherit' }).on('close', resolve);
  });

  // Run tests for each UI
  for (const uiType of ['rust', 'react']) {
    console.log(colors.bright + colors.yellow +
      `\n🧪 Running tests against ${uiType.toUpperCase()} UI...` +
      colors.reset
    );

    for (const scenario of TEST_SCENARIOS) {
      process.stdout.write(
        colors.dim + `  Testing "${scenario.name}"... ` + colors.reset
      );

      const result = await runTest(uiType, `tests/${scenario.file}`, scenario.testName);
      results[uiType][scenario.name] = result;

      if (result.success) {
        console.log(colors.green + '✅' + colors.reset);
      } else {
        console.log(colors.red + '❌' + colors.reset);
      }
    }
  }

  // Print comparison table
  printResultsTable();

  // Exit with appropriate code
  const hasFailures = Object.values(results.rust).some(r => r && !r.success) ||
                     Object.values(results.react).some(r => r && !r.success);
  process.exit(hasFailures ? 1 : 0);
}

// Run if executed directly
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch(console.error);
}

export { runTest, TEST_SCENARIOS };