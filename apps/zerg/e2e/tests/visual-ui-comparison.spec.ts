import { test, expect } from '@playwright/test';
import OpenAI from 'openai';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

/**
 * Enhanced Visual Testing with AI Analysis
 *
 * This test suite captures screenshots of both Rust and React UIs,
 * then uses OpenAI's vision API to provide detailed comparison analysis
 * for identifying differences and improvements needed.
 */

// Initialize OpenAI client
const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
});

interface UIVariant {
  name: string;
  url: string;
  description: string;
}

const UI_VARIANTS: UIVariant[] = [
  {
    name: 'rust-dashboard',
    url: '/',
    description: 'Legacy Rust/WASM UI (target to match)'
  },
  {
    name: 'react-dashboard',
    url: '/react/index.html',
    description: 'New React Prototype UI (needs alignment)'
  }
];

/**
 * Convert screenshot to base64 for OpenAI API
 */
function imageToBase64(buffer: Buffer): string {
  return buffer.toString('base64');
}

/**
 * AI-powered UI comparison analysis
 */
async function analyzeUIComparison(
  rustScreenshot: Buffer,
  reactScreenshot: Buffer,
  testInfo: any
): Promise<string> {
  const comparisonPrompt = `
You are a UI/UX expert tasked with analyzing two user interface screenshots to identify differences and provide actionable recommendations.

**Context**:
- Image 1: Rust/WASM UI (LEGACY - this is the target design to match)
- Image 2: React UI (NEW - this needs to be updated to match the legacy design)

**Analysis Required**:

1. **Layout Structure**: Compare overall page layout, grid systems, spacing
2. **Visual Hierarchy**: Analyze how information is prioritized and organized
3. **Color Scheme**: Exact color differences (provide hex codes when possible)
4. **Typography**: Font families, sizes, weights, line spacing
5. **Component Styling**: Buttons, forms, tables, navigation elements
6. **Interactive Elements**: Hover states, focus indicators, active states
7. **Spacing & Alignment**: Margins, padding, element positioning
8. **Missing/Extra Features**: Elements present in one but not the other

**Output Format**:
## Critical Differences (High Priority)
- [Specific, actionable items that break visual consistency]

## Styling Inconsistencies (Medium Priority)
- [Color, typography, spacing differences]

## Minor Adjustments (Low Priority)
- [Polish and refinement items]

## Implementation Recommendations
- [Specific CSS/styling changes needed]

## Accessibility Considerations
- [Any accessibility issues noticed]

Be extremely detailed and specific. Provide exact measurements, color codes, and CSS property suggestions where possible.
`;

  try {
    const response = await openai.chat.completions.create({
      model: "gpt-4o",
      messages: [
        {
          role: "user",
          content: [
            {
              type: "text",
              text: comparisonPrompt
            },
            {
              type: "text",
              text: "RUST UI (Legacy - Target Design):"
            },
            {
              type: "image_url",
              image_url: {
                url: `data:image/png;base64,${imageToBase64(rustScreenshot)}`,
                detail: "high"
              }
            },
            {
              type: "text",
              text: "REACT UI (New - Needs Updates):"
            },
            {
              type: "image_url",
              image_url: {
                url: `data:image/png;base64,${imageToBase64(reactScreenshot)}`,
                detail: "high"
              }
            }
          ]
        }
      ],
      max_tokens: 2500,
      temperature: 0.1 // Lower temperature for more consistent analysis
    });

    const analysis = response.choices[0].message.content || '';

    // Save detailed report
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const reportDir = path.join(__dirname, '..', 'visual-reports');

    if (!fs.existsSync(reportDir)) {
      fs.mkdirSync(reportDir, { recursive: true });
    }

    const reportPath = path.join(reportDir, `ui-comparison-${timestamp}.md`);
    const report = `# UI Comparison Analysis Report

**Generated**: ${new Date().toISOString()}
**Test**: ${testInfo.title}
**Model**: ${response.model}

## Screenshots Analyzed
- **Rust UI**: Legacy target design
- **React UI**: New prototype requiring updates

${analysis}

## Technical Details
- **Token Usage**: ${JSON.stringify(response.usage, null, 2)}
- **Analysis Temperature**: 0.1 (focused on consistency)
- **Detail Level**: High resolution analysis
`;

    fs.writeFileSync(reportPath, report);

    // Also attach to Playwright test results
    await testInfo.attach('ui-comparison-report', {
      path: reportPath,
      contentType: 'text/markdown'
    });

    console.log(`📄 Detailed report saved: ${reportPath}`);

    return analysis;

  } catch (error) {
    console.error('❌ AI analysis failed:', error);
    return `Analysis failed: ${error instanceof Error ? error.message : 'Unknown error'}`;
  }
}

test.describe('Visual UI Comparison with AI Analysis', () => {
  test.beforeEach(async ({ page }) => {
    // Ensure we're testing against a running local server
    // The UI switch page hosts both variants
    await page.goto('http://localhost:47200/ui-switch.html');
    await page.waitForLoadState('networkidle');
  });

  test('capture and compare Rust vs React dashboards', async ({ page }, testInfo) => {
    const screenshots: { [key: string]: Buffer } = {};

    console.log('🔄 Capturing screenshots of both UI variants...');

    // Capture screenshots of both UI variants
    for (const variant of UI_VARIANTS) {
      console.log(`📸 Capturing ${variant.name}: ${variant.description}`);

      // Navigate to the specific UI
      await page.goto(`http://localhost:47200${variant.url}`);
      await page.waitForLoadState('networkidle');

      // Wait for UI to fully render
      await page.waitForTimeout(2000);

      // Take full page screenshot
      const screenshot = await page.screenshot({
        fullPage: true,
        animations: 'disabled' // Disable animations for consistent comparison
      });

      screenshots[variant.name] = screenshot;

      // Save individual screenshots for reference
      const screenshotPath = `visual-comparison/${variant.name}-${Date.now()}.png`;
      await testInfo.attach(`${variant.name}-screenshot`, {
        body: screenshot,
        contentType: 'image/png'
      });
    }

    // Verify we captured both screenshots
    expect(screenshots['rust-dashboard']).toBeDefined();
    expect(screenshots['react-dashboard']).toBeDefined();

    // Perform AI-powered comparison analysis
    console.log('🤖 Analyzing UI differences with OpenAI Vision API...');

    const analysis = await analyzeUIComparison(
      screenshots['rust-dashboard'],
      screenshots['react-dashboard'],
      testInfo
    );

    // Attach analysis to test results
    await testInfo.attach('ai-analysis', {
      body: analysis,
      contentType: 'text/plain'
    });

    console.log('✅ AI Analysis Summary:');
    console.log('─'.repeat(60));
    console.log(analysis.slice(0, 500) + '...');
    console.log('─'.repeat(60));

    // The test passes if we successfully captured and analyzed both UIs
    // The actual differences are documented in the attached reports
    expect(analysis.length).toBeGreaterThan(100); // Ensure we got substantial analysis
  });

  test('responsive comparison across viewports', async ({ page }, testInfo) => {
    const viewports = [
      { width: 1920, height: 1080, name: 'desktop' },
      { width: 1024, height: 768, name: 'tablet' },
      { width: 375, height: 667, name: 'mobile' }
    ];

    for (const viewport of viewports) {
      console.log(`📱 Testing ${viewport.name} viewport: ${viewport.width}x${viewport.height}`);

      await page.setViewportSize({ width: viewport.width, height: viewport.height });

      const viewportScreenshots: { [key: string]: Buffer } = {};

      // Capture both UIs at this viewport
      for (const variant of UI_VARIANTS) {
        await page.goto(`http://localhost:47200${variant.url}`);
        await page.waitForLoadState('networkidle');
        await page.waitForTimeout(1000);

        const screenshot = await page.screenshot({
          fullPage: true,
          animations: 'disabled'
        });

        viewportScreenshots[variant.name] = screenshot;

        await testInfo.attach(`${variant.name}-${viewport.name}`, {
          body: screenshot,
          contentType: 'image/png'
        });
      }

      // Quick responsive analysis for this viewport
      if (viewportScreenshots['rust-dashboard'] && viewportScreenshots['react-dashboard']) {
        const responsivePrompt = `Analyze responsive design differences at ${viewport.width}x${viewport.height} (${viewport.name}). Focus on layout adaptation, element scaling, and mobile usability.`;

        try {
          const response = await openai.chat.completions.create({
            model: "gpt-4o",
            messages: [
              {
                role: "user",
                content: [
                  { type: "text", text: responsivePrompt },
                  {
                    type: "image_url",
                    image_url: {
                      url: `data:image/png;base64,${imageToBase64(viewportScreenshots['rust-dashboard'])}`,
                      detail: "high"
                    }
                  },
                  {
                    type: "image_url",
                    image_url: {
                      url: `data:image/png;base64,${imageToBase64(viewportScreenshots['react-dashboard'])}`,
                      detail: "high"
                    }
                  }
                ]
              }
            ],
            max_tokens: 1000
          });

          console.log(`📱 ${viewport.name} Analysis:`, response.choices[0].message.content?.slice(0, 200) + '...');
        } catch (error) {
          console.warn(`⚠️  Responsive analysis failed for ${viewport.name}:`, error);
        }
      }
    }
  });
});