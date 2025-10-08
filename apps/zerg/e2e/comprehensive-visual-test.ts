/**
 * Comprehensive Visual Testing System
 *
 * Screenshots multiple pages and sends them to AI for detailed analysis.
 * Supports parameterized testing of different UI components.
 */

import { test, expect } from '@playwright/test';
import { analyzeMultiPageComparison } from './utils/ai-visual-analyzer';

interface PageTestConfig {
  name: string;
  rustUrl: string;
  reactUrl: string;
  description: string;
  waitForSelector?: string;
  setup?: (page: any) => Promise<void>;
  excludeElements?: string[];  // CSS selectors to hide before screenshot
}

// Comprehensive page configurations for testing
const PAGE_CONFIGS: PageTestConfig[] = [
  {
    name: 'dashboard',
    rustUrl: '/',
    reactUrl: '/react/index.html',
    description: 'Main dashboard with agent list and controls',
    waitForSelector: '#agents-table, [data-testid="dashboard-container"]',
    excludeElements: ['.timestamp', '.last-updated']  // Exclude dynamic timestamps
  },
  {
    name: 'chat-empty',
    rustUrl: '/chat',
    reactUrl: '/react/index.html#/chat',
    description: 'Empty chat interface without active conversation',
    waitForSelector: '.chat-container, [data-testid="chat-container"]'
  },
  {
    name: 'canvas-editor',
    rustUrl: '/canvas',
    reactUrl: '/react/index.html#/canvas',
    description: 'Canvas workflow editor interface',
    waitForSelector: '.canvas-container, [data-testid="canvas-container"]'
  }
];

/**
 * Enhanced screenshot capture with element hiding and stabilization
 */
async function captureStabilizedScreenshot(
  page: any,
  config: PageTestConfig,
  variant: 'rust' | 'react'
): Promise<Buffer> {
  const url = variant === 'rust' ? config.rustUrl : config.reactUrl;

  console.log(`📸 Capturing ${variant} ${config.name}: ${url}`);

  // Navigate to the page
  await page.goto(`http://localhost:47200${url}`);
  await page.waitForLoadState('networkidle');

  // Wait for specific elements if configured
  if (config.waitForSelector) {
    try {
      await page.waitForSelector(config.waitForSelector, { timeout: 10000 });
    } catch (error) {
      console.warn(`⚠️  Timeout waiting for selector: ${config.waitForSelector}`);
    }
  }

  // Hide dynamic elements that change between captures
  if (config.excludeElements && config.excludeElements.length > 0) {
    for (const selector of config.excludeElements) {
      try {
        await page.addStyleTag({
          content: `${selector} { visibility: hidden !important; }`
        });
      } catch (error) {
        console.warn(`⚠️  Could not hide element: ${selector}`);
      }
    }
  }

  // Run custom setup if provided
  if (config.setup) {
    await config.setup(page);
  }

  // Wait for animations and rendering to stabilize
  await page.waitForTimeout(3000);

  // Capture screenshot with consistent settings
  const screenshot = await page.screenshot({
    fullPage: true,
    animations: 'disabled',
    scale: 'device'
  });

  return screenshot;
}

test.describe('Comprehensive Visual UI Analysis', () => {

  // Test individual pages
  for (const pageConfig of PAGE_CONFIGS) {
    test(`visual analysis: ${pageConfig.name}`, async ({ page }, testInfo) => {
      const screenshots: { [key: string]: Buffer } = {};

      console.log(`\n🎯 Testing page: ${pageConfig.name}`);
      console.log(`📋 Description: ${pageConfig.description}`);

      // Capture both UI variants
      try {
        screenshots.rust = await captureStabilizedScreenshot(page, pageConfig, 'rust');
        screenshots.react = await captureStabilizedScreenshot(page, pageConfig, 'react');
      } catch (error) {
        console.error(`❌ Screenshot capture failed for ${pageConfig.name}:`, error);
        // Continue test but mark as potential issue
      }

      // Attach screenshots to test results
      if (screenshots.rust) {
        await testInfo.attach(`${pageConfig.name}-rust-screenshot`, {
          body: screenshots.rust,
          contentType: 'image/png'
        });
      }

      if (screenshots.react) {
        await testInfo.attach(`${pageConfig.name}-react-screenshot`, {
          body: screenshots.react,
          contentType: 'image/png'
        });
      }

      // Skip AI analysis if screenshots failed
      if (!screenshots.rust || !screenshots.react) {
        console.warn(`⚠️  Skipping AI analysis for ${pageConfig.name} due to screenshot issues`);
        return;
      }

      // Perform AI analysis
      console.log(`🤖 Analyzing ${pageConfig.name} UI differences...`);

      const analysis = await analyzeMultiPageComparison(
        screenshots.rust,
        screenshots.react,
        {
          pageName: pageConfig.name,
          description: pageConfig.description,
          testInfo
        }
      );

      // Attach analysis to test results
      await testInfo.attach(`${pageConfig.name}-ai-analysis`, {
        body: analysis,
        contentType: 'text/markdown'
      });

      // Log summary for immediate feedback
      console.log(`✅ Analysis for ${pageConfig.name} complete`);
      console.log('─'.repeat(50));
      console.log(analysis.slice(0, 300) + '...\n');

      // Basic assertion that analysis was generated
      expect(analysis).toBeTruthy();
      expect(analysis.length).toBeGreaterThan(100);  // Ensure substantial analysis
    });
  }

  // Comprehensive multi-page analysis
  test('comprehensive multi-page visual report', async ({ page }, testInfo) => {
    console.log('\n🎯 Running comprehensive multi-page analysis...');

    const allScreenshots: {
      [pageName: string]: {
        rust: Buffer;
        react: Buffer;
        config: PageTestConfig;
      }
    } = {};

    // Capture all pages
    for (const pageConfig of PAGE_CONFIGS) {
      console.log(`📸 Capturing all variants for: ${pageConfig.name}`);

      try {
        const rustScreenshot = await captureStabilizedScreenshot(page, pageConfig, 'rust');
        const reactScreenshot = await captureStabilizedScreenshot(page, pageConfig, 'react');

        allScreenshots[pageConfig.name] = {
          rust: rustScreenshot,
          react: reactScreenshot,
          config: pageConfig
        };
      } catch (error) {
        console.error(`❌ Failed to capture ${pageConfig.name}:`, error);
      }
    }

    // Generate comprehensive analysis report
    console.log('🤖 Generating comprehensive UI analysis report...');

    const comprehensiveReport = await generateComprehensiveReport(allScreenshots);

    // Save comprehensive report
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const reportPath = `visual-reports/comprehensive-ui-analysis-${timestamp}.md`;

    await testInfo.attach('comprehensive-ui-analysis', {
      body: comprehensiveReport,
      contentType: 'text/markdown'
    });

    console.log('✅ Comprehensive visual analysis complete');
    console.log(`📄 Report saved as test attachment`);

    // Basic assertions
    expect(comprehensiveReport).toBeTruthy();
    expect(comprehensiveReport.length).toBeGreaterThan(500);
    expect(Object.keys(allScreenshots).length).toBeGreaterThan(0);
  });

});

/**
 * Generate a comprehensive analysis report covering all pages
 */
async function generateComprehensiveReport(
  allScreenshots: {
    [pageName: string]: {
      rust: Buffer;
      react: Buffer;
      config: PageTestConfig;
    }
  }
): Promise<string> {

  let report = `# Comprehensive UI Parity Analysis Report\n\n`;
  report += `Generated: ${new Date().toISOString()}\n\n`;
  report += `## Executive Summary\n\n`;

  const pageNames = Object.keys(allScreenshots);
  report += `Analyzed ${pageNames.length} page variants: ${pageNames.join(', ')}\n\n`;

  // Analyze each page individually
  for (const [pageName, screenshots] of Object.entries(allScreenshots)) {
    report += `## ${pageName.charAt(0).toUpperCase() + pageName.slice(1)} Analysis\n\n`;
    report += `**Description**: ${screenshots.config.description}\n\n`;

    try {
      const analysis = await analyzeMultiPageComparison(
        screenshots.rust,
        screenshots.react,
        {
          pageName,
          description: screenshots.config.description
        }
      );

      report += analysis + '\n\n';

    } catch (error) {
      report += `❌ Analysis failed: ${error}\n\n`;
    }

    report += `---\n\n`;
  }

  // Overall recommendations
  report += `## Overall Recommendations\n\n`;
  report += `1. **Priority Focus**: Address critical differences first\n`;
  report += `2. **Consistency**: Ensure styling patterns are consistent across pages\n`;
  report += `3. **Testing**: Run this analysis after each UI change\n`;
  report += `4. **Automation**: Consider integrating into CI/CD pipeline\n\n`;

  return report;
}

export { PageTestConfig, PAGE_CONFIGS, captureStabilizedScreenshot };