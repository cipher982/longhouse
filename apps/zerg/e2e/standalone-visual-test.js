#!/usr/bin/env node
/**
 * Standalone Visual Testing Script
 *
 * Run this with servers already started to avoid Playwright webServer conflicts.
 * Usage: node standalone-visual-test.js
 */

import { chromium } from 'playwright';
import OpenAI from 'openai';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Initialize OpenAI
const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
});

console.log('🚀 Starting standalone visual testing...\n');

// Configuration
const FRONTEND_URL = process.env.FRONTEND_URL || 'http://localhost:47200';
const REPORT_DIR = path.join(__dirname, 'visual-reports');

// Ensure report directory exists
if (!fs.existsSync(REPORT_DIR)) {
  fs.mkdirSync(REPORT_DIR, { recursive: true });
}

/**
 * Capture screenshots of both UIs
 */
async function captureScreenshots() {
  console.log('📸 Launching browser...');
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 }
  });
  const page = await context.newPage();

  const screenshots = {};

  // Capture Rust UI
  console.log('📸 Capturing Rust/WASM UI (/)...');
  await page.goto(`${FRONTEND_URL}/`);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(3000); // Let animations settle
  screenshots.rust = await page.screenshot({
    fullPage: true,
    animations: 'disabled'
  });
  console.log(`✅ Rust UI captured (${screenshots.rust.length} bytes)`);

  // Capture React UI
  console.log('📸 Capturing React UI (/react/index.html)...');
  await page.goto(`${FRONTEND_URL}/react/index.html`);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(3000); // Let animations settle
  screenshots.react = await page.screenshot({
    fullPage: true,
    animations: 'disabled'
  });
  console.log(`✅ React UI captured (${screenshots.react.length} bytes)\n`);

  await browser.close();

  // Save screenshots
  const timestamp = Date.now();
  const rustPath = path.join(REPORT_DIR, `rust-ui-${timestamp}.png`);
  const reactPath = path.join(REPORT_DIR, `react-ui-${timestamp}.png`);
  fs.writeFileSync(rustPath, screenshots.rust);
  fs.writeFileSync(reactPath, screenshots.react);
  console.log(`💾 Screenshots saved to ${REPORT_DIR}\n`);

  return screenshots;
}

/**
 * Analyze screenshots with OpenAI Vision API
 */
async function analyzeWithAI(screenshots) {
  console.log('🤖 Analyzing UI differences with GPT-4o Vision...\n');

  const prompt = `
You are a senior UI/UX designer analyzing two implementations of the same web application.

**Context**:
- Image 1: Rust/WASM UI (LEGACY - this is the target design to match)
- Image 2: React UI (NEW - this needs to be updated to match the legacy design)

**Analysis Required**:
1. **Critical Differences** - Issues that break functionality or brand consistency
2. **Styling Inconsistencies** - Visual differences affecting user experience
3. **Layout & Spacing** - Grid, margins, padding, alignment differences
4. **Typography** - Font families, sizes, weights, line-height
5. **Color Scheme** - Exact color differences (provide hex codes)
6. **Interactive Elements** - Buttons, forms, navigation styling
7. **Missing/Extra Features** - Elements present in one but not the other

**Output Format**:
## Critical Differences (Must Fix)
- [Specific actionable items]

## Styling Inconsistencies (Should Fix)
- [Color, typography, spacing differences]

## Implementation Recommendations
- [Exact CSS changes needed with property names and values]

Be extremely detailed and specific. Provide exact measurements and CSS suggestions.
`;

  try {
    const response = await openai.chat.completions.create({
      model: "gpt-4o",
      messages: [
        {
          role: "user",
          content: [
            { type: "text", text: prompt },
            { type: "text", text: "**RUST UI (Legacy - Target Design):**" },
            {
              type: "image_url",
              image_url: {
                url: `data:image/png;base64,${screenshots.rust.toString('base64')}`,
                detail: "high"
              }
            },
            { type: "text", text: "**REACT UI (New - Needs Updates):**" },
            {
              type: "image_url",
              image_url: {
                url: `data:image/png;base64,${screenshots.react.toString('base64')}`,
                detail: "high"
              }
            }
          ]
        }
      ],
      max_tokens: 3000,
      temperature: 0.1
    });

    const analysis = response.choices[0].message.content || '';

    console.log('✅ AI Analysis Complete\n');
    console.log('─'.repeat(70));
    console.log('ANALYSIS PREVIEW:');
    console.log('─'.repeat(70));
    console.log(analysis.slice(0, 500) + '...\n');
    console.log('─'.repeat(70));

    // Save full analysis
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const reportPath = path.join(REPORT_DIR, `visual-analysis-${timestamp}.md`);

    const fullReport = `# Visual UI Analysis Report

**Generated**: ${new Date().toISOString()}
**Model**: gpt-4o
**Token Usage**: ${JSON.stringify(response.usage, null, 2)}

## Analysis

${analysis}

---

*Generated by standalone-visual-test.js*
`;

    fs.writeFileSync(reportPath, fullReport);
    console.log(`\n📄 Full report saved: ${reportPath}\n`);

    return { analysis, usage: response.usage };

  } catch (error) {
    console.error('❌ AI analysis failed:', error.message);
    throw error;
  }
}

/**
 * Main execution
 */
async function main() {
  try {
    // Check if frontend is accessible
    console.log(`🔍 Checking frontend at ${FRONTEND_URL}...`);
    const response = await fetch(`${FRONTEND_URL}/`);
    if (!response.ok) {
      throw new Error(`Frontend not responding at ${FRONTEND_URL}`);
    }
    console.log('✅ Frontend is accessible\n');

    // Capture screenshots
    const screenshots = await captureScreenshots();

    // Analyze with AI
    const { analysis, usage } = await analyzeWithAI(screenshots);

    console.log('🎉 Visual testing complete!\n');
    console.log('API Usage:');
    console.log(`  - Prompt tokens: ${usage.prompt_tokens}`);
    console.log(`  - Completion tokens: ${usage.completion_tokens}`);
    console.log(`  - Total tokens: ${usage.total_tokens}\n`);

  } catch (error) {
    console.error('\n❌ Visual testing failed:', error.message);
    process.exit(1);
  }
}

// Run if executed directly
main();
