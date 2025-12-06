#!/usr/bin/env node

/**
 * Standalone Visual Test Demo
 *
 * This demonstrates the complete AI-powered visual testing system
 * without the complexity of Playwright's test runner configuration.
 */

import { chromium } from 'playwright';
import AIVisualAnalyzer, { ZERG_UI_VARIANTS } from './utils/ai-visual-analyzer.ts';
import fs from 'fs';

async function runVisualTest() {
  console.log('🚀 AI-Powered Visual Testing Demo\n');

  // Check for OpenAI API key
  if (!process.env.OPENAI_API_KEY) {
    console.error('❌ Missing OPENAI_API_KEY environment variable');
    process.exit(1);
  }

  // Launch browser
  console.log('🌐 Launching browser...');
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 }
  });
  const page = await context.newPage();

  try {
    // Initialize AI analyzer
    const analyzer = new AIVisualAnalyzer('demo-visual-test');

    // Test server availability
    console.log('🔍 Checking server availability...');
    try {
      await page.goto('http://localhost:47200/ui-switch.html');
      await page.waitForLoadState('networkidle');
      console.log('✅ Server accessible at http://localhost:47200');
    } catch (error) {
      console.error('❌ Server not accessible. Make sure to run `make start` first');
      throw error;
    }

    // Capture screenshots of both UI variants
    console.log('\n📸 Capturing UI Screenshots...');
    const screenshots = await analyzer.captureUIVariants(page, ZERG_UI_VARIANTS);

    console.log(`✅ Captured ${Object.keys(screenshots).length} screenshots`);
    Object.keys(screenshots).forEach(name => {
      const size = (screenshots[name].length / 1024).toFixed(1);
      console.log(`   → ${name}: ${size} KB`);
    });

    // Perform AI analysis
    console.log('\n🤖 Analyzing UI Differences with OpenAI...');
    const analysisOptions = {
      model: 'gpt-4o',
      detailLevel: 'high',
      focusAreas: [
        'Layout Structure & Grid Systems',
        'Color Scheme & Brand Consistency',
        'Typography & Visual Hierarchy',
        'Component Styling (buttons, forms, tables)',
        'Spacing & Alignment',
        'Interactive Elements & States',
        'Missing or Extra Features'
      ]
    };

    const result = await analyzer.analyzeUIComparison(
      screenshots,
      ZERG_UI_VARIANTS,
      analysisOptions
    );

    console.log('✅ Analysis Complete!');
    console.log(`📄 Full Report: ${result.reportPath}`);
    console.log(`💰 Tokens Used: ${result.usage?.total_tokens || 'N/A'}`);

    // Show analysis preview
    console.log('\n📋 Analysis Preview:');
    console.log('─'.repeat(60));
    const preview = result.analysis.slice(0, 800);
    console.log(preview + (result.analysis.length > 800 ? '...' : ''));
    console.log('─'.repeat(60));

    // Test responsive analysis
    console.log('\n📱 Testing Responsive Analysis...');
    const viewports = [
      { width: 1920, height: 1080, name: 'desktop' },
      { width: 768, height: 1024, name: 'tablet' },
      { width: 375, height: 667, name: 'mobile' }
    ];

    const responsiveResults = await analyzer.analyzeResponsiveDesign(
      page,
      ZERG_UI_VARIANTS,
      viewports
    );

    console.log('✅ Responsive Analysis Complete!');
    Object.keys(responsiveResults).forEach(viewport => {
      const result = responsiveResults[viewport];
      console.log(`   → ${viewport}: ${result.reportPath.split('/').pop()}`);
    });

    // Generate summary report
    const summaryPath = generateSummaryReport(result, responsiveResults);
    console.log(`\n📊 Summary Report: ${summaryPath}`);

    console.log('\n🎉 Visual Testing Demo Complete!');
    console.log('\nKey Outputs:');
    console.log(`- Main Analysis: ${result.reportPath}`);
    console.log(`- Summary Report: ${summaryPath}`);
    console.log('- Screenshots saved in visual-reports/demo-visual-test/');

  } catch (error) {
    console.error('\n❌ Test failed:', error.message);
    throw error;
  } finally {
    await browser.close();
  }
}

function generateSummaryReport(mainResult, responsiveResults) {
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  const summaryPath = `visual-reports/demo-visual-test/SUMMARY-${timestamp}.md`;

  const summary = `# Visual Testing Demo Summary

**Generated**: ${new Date().toISOString()}
**Test Type**: AI-Powered UI Comparison
**Variants Tested**: Rust/WASM vs React Dashboard

## Results Overview

### Main Analysis
- **Report**: ${mainResult.reportPath.split('/').pop()}
- **Tokens Used**: ${mainResult.usage?.total_tokens || 'N/A'}
- **Analysis Length**: ${mainResult.analysis.length} characters

### Responsive Analysis
${Object.keys(responsiveResults).map(viewport => {
  const result = responsiveResults[viewport];
  return `- **${viewport}**: ${result.reportPath.split('/').pop()} (${result.usage?.total_tokens || 'N/A'} tokens)`;
}).join('\n')}

## Key Findings Preview

${mainResult.analysis.split('\n').slice(0, 20).join('\n')}

---

## Next Steps

1. Review detailed analysis reports for specific actionable items
2. Prioritize critical differences for React UI alignment
3. Implement recommended changes incrementally
4. Re-run visual tests to validate improvements

*This summary was generated by the AI Visual Testing Framework*
`;

  fs.writeFileSync(summaryPath, summary);
  return summaryPath;
}

// Run the demo
runVisualTest()
  .then(() => {
    console.log('\n🏁 Demo completed successfully');
    process.exit(0);
  })
  .catch((error) => {
    console.error('\n💥 Demo failed:', error);
    process.exit(1);
  });
