/**
 * AI-Powered Visual Testing Utilities
 *
 * This module extends the existing visual testing framework with OpenAI's vision API
 * to provide intelligent analysis of UI differences and generate actionable reports.
 */

import { Page, TestInfo } from '@playwright/test';
import OpenAI from 'openai';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Initialize OpenAI client
const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
});

export interface UIVariant {
  name: string;
  url: string;
  description: string;
  waitFor?: string | number;
  setup?: (page: Page) => Promise<void>;
}

export interface AnalysisOptions {
  model?: string;
  maxTokens?: number;
  temperature?: number;
  detailLevel?: 'low' | 'medium' | 'high';
  focusAreas?: string[];
}

export interface ComparisonResult {
  analysis: string;
  reportPath: string;
  screenshots: { [variant: string]: Buffer };
  usage?: any;
  timestamp: string;
}

/**
 * Enhanced Visual Analyzer with AI capabilities
 */
export class AIVisualAnalyzer {
  private reportDir: string;

  constructor(testName: string = 'visual-analysis') {
    this.reportDir = path.join(__dirname, '..', 'visual-reports', testName);
    this.ensureReportDirectory();
  }

  private ensureReportDirectory(): void {
    if (!fs.existsSync(this.reportDir)) {
      fs.mkdirSync(this.reportDir, { recursive: true });
    }
  }

  /**
   * Capture screenshots of multiple UI variants
   */
  async captureUIVariants(
    page: Page,
    variants: UIVariant[],
    testInfo?: TestInfo
  ): Promise<{ [variantName: string]: Buffer }> {
    const screenshots: { [key: string]: Buffer } = {};

    console.log(`📸 Capturing ${variants.length} UI variant screenshots...`);

    for (const variant of variants) {
      console.log(`   → ${variant.name}: ${variant.description}`);

      // Navigate to variant
      await page.goto(`http://localhost:47200${variant.url}`);
      await page.waitForLoadState('networkidle');

      // Run setup if provided
      if (variant.setup) {
        await variant.setup(page);
      }

      // Wait for specific element or time
      if (variant.waitFor) {
        if (typeof variant.waitFor === 'string') {
          await page.waitForSelector(variant.waitFor, { timeout: 10000 });
        } else {
          await page.waitForTimeout(variant.waitFor);
        }
      } else {
        await page.waitForTimeout(2000); // Default wait for rendering
      }

      // Capture screenshot
      const screenshot = await page.screenshot({
        fullPage: true,
        animations: 'disabled'
      });

      screenshots[variant.name] = screenshot;

      // Attach to test if provided
      if (testInfo) {
        await testInfo.attach(`${variant.name}-screenshot`, {
          body: screenshot,
          contentType: 'image/png'
        });
      }

      // Save to file system
      const screenshotPath = path.join(this.reportDir, `${variant.name}-${Date.now()}.png`);
      fs.writeFileSync(screenshotPath, screenshot);
    }

    console.log(`✅ Captured ${Object.keys(screenshots).length} screenshots`);
    return screenshots;
  }

  /**
   * Generate AI analysis prompt based on focus areas
   */
  private generateAnalysisPrompt(
    variants: UIVariant[],
    options: AnalysisOptions = {}
  ): string {
    const { focusAreas = [], detailLevel = 'high' } = options;

    const basePrompt = `
You are a senior UI/UX designer and frontend developer expert. Analyze these ${variants.length} interface screenshots to identify differences and provide actionable recommendations.

**Context**:
${variants.map((v, i) => `- Image ${i + 1}: ${v.description}`).join('\n')}

**Analysis Framework**:
`;

    const analysisAreas = focusAreas.length > 0 ? focusAreas : [
      'Layout Structure & Grid Systems',
      'Color Scheme & Brand Consistency',
      'Typography Hierarchy',
      'Component Styling (buttons, forms, navigation)',
      'Spacing & Alignment',
      'Interactive Elements & States',
      'Responsive Behavior',
      'Accessibility Considerations',
      'Missing or Extra Features',
      'Performance Implications'
    ];

    const detailInstructions = {
      low: 'Provide high-level overview with 3-5 key differences.',
      medium: 'Provide detailed analysis with specific recommendations.',
      high: 'Provide comprehensive analysis with exact measurements, color codes, and CSS suggestions.'
    };

    return `${basePrompt}

${analysisAreas.map(area => `### ${area}`).join('\n')}

**Output Requirements**:
- ${detailInstructions[detailLevel]}
- Use markdown formatting for readability
- Provide specific, actionable recommendations
- Include priority levels (Critical/Important/Nice-to-have)
- Suggest exact CSS changes where possible

**Format**:
## Executive Summary
[Brief overview of key findings]

## Critical Differences (Must Fix)
[Issues that break functionality or brand consistency]

## Important Improvements (Should Fix)
[Styling and UX inconsistencies]

## Enhancement Opportunities (Nice to Have)
[Polish and optimization suggestions]

## Implementation Guide
[Specific technical recommendations]

Be extremely detailed and practical in your analysis.`;
  }

  /**
   * Analyze UI differences using OpenAI Vision API
   */
  async analyzeUIComparison(
    screenshots: { [variantName: string]: Buffer },
    variants: UIVariant[],
    options: AnalysisOptions = {},
    testInfo?: TestInfo
  ): Promise<ComparisonResult> {
    const {
      model = 'gpt-4o',
      maxTokens = 3000,
      temperature = 0.1
    } = options;

    console.log('🤖 Generating AI-powered visual analysis...');

    try {
      // Prepare content for API
      const content: any[] = [
        {
          type: 'text',
          text: this.generateAnalysisPrompt(variants, options)
        }
      ];

      // Add screenshots to content
      variants.forEach((variant, index) => {
        const screenshot = screenshots[variant.name];
        if (screenshot) {
          content.push(
            {
              type: 'text',
              text: `**${variant.description}**:`
            },
            {
              type: 'image_url',
              image_url: {
                url: `data:image/png;base64,${screenshot.toString('base64')}`,
                detail: 'high'
              }
            }
          );
        }
      });

      // Make OpenAI API call
      const response = await openai.chat.completions.create({
        model,
        messages: [{ role: 'user', content }],
        max_tokens: maxTokens,
        temperature
      });

      const analysis = response.choices[0].message.content || '';
      const timestamp = new Date().toISOString();

      // Generate comprehensive report
      const reportPath = await this.generateReport({
        analysis,
        variants,
        screenshots,
        usage: response.usage,
        timestamp,
        options
      });

      // Attach to test if provided
      if (testInfo) {
        await testInfo.attach('ai-visual-analysis', {
          path: reportPath,
          contentType: 'text/markdown'
        });
      }

      console.log('✅ AI analysis complete');
      console.log(`📄 Report: ${reportPath}`);

      return {
        analysis,
        reportPath,
        screenshots,
        usage: response.usage,
        timestamp
      };

    } catch (error) {
      console.error('❌ AI analysis failed:', error);
      throw new Error(`AI analysis failed: ${error instanceof Error ? error.message : 'Unknown error'}`);
    }
  }

  /**
   * Generate comprehensive markdown report
   */
  private async generateReport(data: {
    analysis: string;
    variants: UIVariant[];
    screenshots: { [key: string]: Buffer };
    usage?: any;
    timestamp: string;
    options: AnalysisOptions;
  }): Promise<string> {
    const { analysis, variants, usage, timestamp, options } = data;

    const reportFilename = `visual-analysis-${timestamp.replace(/[:.]/g, '-')}.md`;
    const reportPath = path.join(this.reportDir, reportFilename);

    const report = `# Visual UI Analysis Report

**Generated**: ${new Date(timestamp).toLocaleString()}
**Analysis Model**: ${options.model || 'gpt-4o'}
**Detail Level**: ${options.detailLevel || 'high'}

## Test Configuration

### UI Variants Analyzed
${variants.map((variant, i) => `
${i + 1}. **${variant.name}**
   - URL: \`${variant.url}\`
   - Description: ${variant.description}
`).join('')}

### Analysis Settings
- **Model**: ${options.model || 'gpt-4o'}
- **Max Tokens**: ${options.maxTokens || 3000}
- **Temperature**: ${options.temperature || 0.1}
- **Focus Areas**: ${options.focusAreas?.join(', ') || 'All standard areas'}

---

## AI Analysis Results

${analysis}

---

## Technical Details

### API Usage Statistics
\`\`\`json
${JSON.stringify(usage, null, 2)}
\`\`\`

### Screenshot Metadata
${Object.keys(data.screenshots).map(name => `- **${name}**: ${data.screenshots[name].length} bytes`).join('\n')}

---

*This report was automatically generated by the AI Visual Testing Framework*
`;

    fs.writeFileSync(reportPath, report);
    return reportPath;
  }

  /**
   * Quick single-screenshot analysis
   */
  async analyzeScreenshot(
    screenshot: Buffer,
    description: string,
    prompt?: string
  ): Promise<string> {
    const defaultPrompt = `Analyze this UI screenshot: ${description}.

    Provide insights on:
    - Overall design quality and consistency
    - Potential usability issues
    - Accessibility considerations
    - Areas for improvement

    Be specific and actionable in your recommendations.`;

    try {
      const response = await openai.chat.completions.create({
        model: 'gpt-4o',
        messages: [
          {
            role: 'user',
            content: [
              {
                type: 'text',
                text: prompt || defaultPrompt
              },
              {
                type: 'image_url',
                image_url: {
                  url: `data:image/png;base64,${screenshot.toString('base64')}`,
                  detail: 'high'
                }
              }
            ]
          }
        ],
        max_tokens: 1500,
        temperature: 0.1
      });

      return response.choices[0].message.content || 'No analysis generated';

    } catch (error) {
      console.error('Screenshot analysis failed:', error);
      return `Analysis failed: ${error instanceof Error ? error.message : 'Unknown error'}`;
    }
  }

  /**
   * Batch analyze multiple viewports
   */
  async analyzeResponsiveDesign(
    page: Page,
    variants: UIVariant[],
    viewports: Array<{ width: number; height: number; name: string }>,
    testInfo?: TestInfo
  ): Promise<{ [viewport: string]: ComparisonResult }> {
    const results: { [viewport: string]: ComparisonResult } = {};

    for (const viewport of viewports) {
      console.log(`📱 Analyzing ${viewport.name} (${viewport.width}x${viewport.height})`);

      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.waitForTimeout(1000);

      const screenshots = await this.captureUIVariants(page, variants, testInfo);

      const responsiveOptions: AnalysisOptions = {
        focusAreas: [
          'Responsive Layout Adaptation',
          'Mobile Usability',
          'Touch Target Sizing',
          'Content Prioritization',
          'Navigation Accessibility'
        ],
        detailLevel: 'medium'
      };

      const result = await this.analyzeUIComparison(
        screenshots,
        variants,
        responsiveOptions,
        testInfo
      );

      results[viewport.name] = result;
    }

    return results;
  }
}

// Default UI variants for the Zerg platform
export const ZERG_UI_VARIANTS: UIVariant[] = [
  {
    name: 'rust-dashboard',
    url: '/',
    description: 'Legacy Rust/WASM UI (target design to match)',
    waitFor: '[data-testid="fiche-dashboard"], .dashboard, main'
  },
  {
    name: 'react-dashboard',
    url: '/react/index.html',
    description: 'New React Prototype UI (requires alignment with legacy)',
    waitFor: '[data-testid="react-dashboard"], .app, main'
  }
];

// Standard viewport configurations
export const STANDARD_VIEWPORTS = [
  { width: 1920, height: 1080, name: 'desktop-large' },
  { width: 1366, height: 768, name: 'desktop-standard' },
  { width: 1024, height: 768, name: 'tablet-landscape' },
  { width: 768, height: 1024, name: 'tablet-portrait' },
  { width: 414, height: 896, name: 'mobile-large' },
  { width: 375, height: 667, name: 'mobile-standard' }
];

/**
 * Simplified function for multi-page comparison analysis
 * Used by comprehensive-visual-test.ts
 */
export async function analyzeMultiPageComparison(
  rustScreenshot: Buffer,
  reactScreenshot: Buffer,
  context: {
    pageName: string;
    description: string;
    testInfo?: any;
  }
): Promise<string> {

  if (!openai) {
    throw new Error('OpenAI API key not configured');
  }

  console.log(`🤖 Analyzing ${context.pageName} UI differences...`);

  const comparisonPrompt = `
You are a UI/UX expert analyzing two screenshots of the same page implemented in different frameworks.

**Context**:
- Page: ${context.pageName}
- Description: ${context.description}
- Image 1: Rust/WASM UI (LEGACY - this is the target design to match)
- Image 2: React UI (NEW - this needs to be updated to match the legacy design)

**Analysis Required**:
1. **Layout Differences**: Compare overall structure, spacing, alignment
2. **Visual Consistency**: Colors, fonts, component styling
3. **Interactive Elements**: Buttons, forms, hover states
4. **Missing/Extra Features**: Elements present in one but not the other
5. **Priority Assessment**: Critical vs. cosmetic differences

**Output Format**:
### ${context.pageName} Analysis

#### Critical Issues (Must Fix)
- [List issues that break functionality or brand consistency]

#### Styling Inconsistencies (Should Fix)
- [List visual differences that affect user experience]

#### Minor Improvements (Nice to Have)
- [List polish items and optimizations]

#### Implementation Recommendations
- [Specific CSS/code changes needed]

Be specific and actionable. Provide exact measurements and CSS suggestions where possible.
`;

  try {
    const response = await openai.chat.completions.create({
      model: "gpt-5.2",
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
              text: `RUST UI (Legacy - Target Design for ${context.pageName}):`
            },
            {
              type: "image_url",
              image_url: {
                url: `data:image/png;base64,${rustScreenshot.toString('base64')}`,
                detail: "high"
              }
            },
            {
              type: "text",
              text: `REACT UI (New - Needs Updates for ${context.pageName}):`
            },
            {
              type: "image_url",
              image_url: {
                url: `data:image/png;base64,${reactScreenshot.toString('base64')}`,
                detail: "high"
              }
            }
          ]
        }
      ],
      max_tokens: 2000,
      temperature: 0.1
    });

    const analysis = response.choices[0].message.content || 'Analysis failed to generate content.';

    // Save detailed report if testInfo provided
    if (context.testInfo) {
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
      const reportDir = path.join(__dirname, '..', 'visual-reports');

      if (!fs.existsSync(reportDir)) {
        fs.mkdirSync(reportDir, { recursive: true });
      }

      const reportPath = path.join(reportDir, `${context.pageName}-analysis-${timestamp}.md`);
      const fullReport = `# ${context.pageName} UI Analysis Report\n\n`;
      const reportContent = fullReport +
                           `**Generated**: ${new Date().toISOString()}\n\n` +
                           `**Page Description**: ${context.description}\n\n` +
                           analysis;

      fs.writeFileSync(reportPath, reportContent);
      console.log(`📄 Analysis report saved: ${reportPath}`);
    }

    return analysis;

  } catch (error) {
    console.error(`❌ AI analysis failed for ${context.pageName}:`, error);
    return `Analysis failed for ${context.pageName}: ${error instanceof Error ? error.message : 'Unknown error'}`;
  }
}

export default AIVisualAnalyzer;
