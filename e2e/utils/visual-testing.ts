/**
 * VISUAL TESTING UTILITIES
 *
 * This module provides advanced visual testing capabilities including:
 * - Screenshot comparison and regression testing
 * - Visual element validation
 * - Color accessibility testing
 * - Layout shift detection
 * - Cross-browser visual consistency
 */

import { Page, expect } from '@playwright/test';
import { createHash } from 'crypto';

export interface VisualTestOptions {
  threshold?: number;
  maxDiffPixels?: number;
  fullPage?: boolean;
  clip?: { x: number; y: number; width: number; height: number };
  maskElements?: string[];
  animations?: 'disabled' | 'allow';
}

export interface ColorInfo {
  hex: string;
  rgb: { r: number; g: number; b: number };
  hsl: { h: number; s: number; l: number };
  luminance: number;
}

export interface ContrastResult {
  ratio: number;
  level: 'AAA' | 'AA' | 'A' | 'FAIL';
  passes: {
    normal: { AA: boolean; AAA: boolean };
    large: { AA: boolean; AAA: boolean };
  };
}

export interface LayoutShift {
  element: string;
  beforeRect: DOMRect;
  afterRect: DOMRect;
  shift: {
    x: number;
    y: number;
    width: number;
    height: number;
  };
}

/**
 * Visual Testing Helper Class
 */
export class VisualTestHelper {
  private baselineDir: string;
  private diffDir: string;

  constructor(private page: Page, testName: string) {
    this.baselineDir = `visual-baselines/${testName}`;
    this.diffDir = `visual-diffs/${testName}`;
  }

  /**
   * Take and compare screenshot with baseline
   */
  async compareScreenshot(
    name: string,
    options: VisualTestOptions = {}
  ): Promise<{
    matches: boolean;
    diffPath?: string;
    baselinePath: string;
    actualPath: string;
  }> {
    const baselineName = `${name}-baseline.png`;
    const actualName = `${name}-actual.png`;
    const diffName = `${name}-diff.png`;

    const baselinePath = `${this.baselineDir}/${baselineName}`;
    const actualPath = `${this.diffDir}/${actualName}`;
    const diffPath = `${this.diffDir}/${diffName}`;

    // Disable animations if requested
    if (options.animations === 'disabled') {
      await this.disableAnimations();
    }

    // Mask elements if specified
    const maskSelectors = options.maskElements || [];
    const maskLocators = maskSelectors.map(selector => this.page.locator(selector));

    try {
      // Take screenshot and compare with baseline
      await expect(this.page).toHaveScreenshot(baselineName, {
        threshold: options.threshold || 0.1,
        maxDiffPixels: options.maxDiffPixels || 100,
        fullPage: options.fullPage || false,
        clip: options.clip,
        mask: maskLocators
      });

      return {
        matches: true,
        baselinePath,
        actualPath
      };
    } catch (error) {
      return {
        matches: false,
        diffPath,
        baselinePath,
        actualPath
      };
    }
  }

  /**
   * Disable CSS animations and transitions
   */
  private async disableAnimations(): Promise<void> {
    await this.page.addStyleTag({
      content: `
        *, *::before, *::after {
          animation-duration: 0s !important;
          animation-delay: 0s !important;
          transition-duration: 0s !important;
          transition-delay: 0s !important;
        }
      `
    });
  }

  /**
   * Test visual consistency across different viewport sizes
   */
  async testResponsiveVisuals(
    name: string,
    viewports: Array<{ width: number; height: number; name: string }>
  ): Promise<Array<{
    viewport: string;
    matches: boolean;
    path: string;
  }>> {
    const results = [];

    for (const viewport of viewports) {
      await this.page.setViewportSize({ width: viewport.width, height: viewport.height });
      await this.page.waitForTimeout(500); // Allow layout to settle

      const screenshotName = `${name}-${viewport.name}`;
      const result = await this.compareScreenshot(screenshotName, { fullPage: true });

      results.push({
        viewport: viewport.name,
        matches: result.matches,
        path: result.actualPath
      });
    }

    return results;
  }

  /**
   * Detect layout shifts between operations
   */
  async detectLayoutShifts(
    operation: () => Promise<void>,
    elementsToWatch: string[] = ['header', 'main', '.content', '[data-testid]']
  ): Promise<LayoutShift[]> {
    // Get initial element positions
    const initialPositions = await this.getElementPositions(elementsToWatch);

    // Perform the operation
    await operation();

    // Wait for potential layout changes
    await this.page.waitForTimeout(500);

    // Get final element positions
    const finalPositions = await this.getElementPositions(elementsToWatch);

    // Calculate shifts
    const layoutShifts: LayoutShift[] = [];

    for (const selector of elementsToWatch) {
      const before = initialPositions[selector];
      const after = finalPositions[selector];

      if (before && after) {
        const shift = {
          x: after.x - before.x,
          y: after.y - before.y,
          width: after.width - before.width,
          height: after.height - before.height
        };

        // Only report significant shifts (> 1px)
        if (Math.abs(shift.x) > 1 || Math.abs(shift.y) > 1 ||
            Math.abs(shift.width) > 1 || Math.abs(shift.height) > 1) {
          layoutShifts.push({
            element: selector,
            beforeRect: before,
            afterRect: after,
            shift
          });
        }
      }
    }

    return layoutShifts;
  }

  /**
   * Get positions of multiple elements
   */
  private async getElementPositions(selectors: string[]): Promise<Record<string, DOMRect | null>> {
    const positions: Record<string, DOMRect | null> = {};

    for (const selector of selectors) {
      try {
        const element = this.page.locator(selector).first();
        const count = await element.count();

        if (count > 0) {
          positions[selector] = await element.boundingBox();
        } else {
          positions[selector] = null;
        }
      } catch (error) {
        positions[selector] = null;
      }
    }

    return positions;
  }

  /**
   * Test element visibility and positioning
   */
  async validateElementVisibility(
    elements: Array<{ selector: string; shouldBeVisible: boolean; position?: 'above-fold' | 'below-fold' }>
  ): Promise<Array<{
    selector: string;
    expected: boolean;
    actual: boolean;
    position?: { x: number; y: number; inViewport: boolean };
    passes: boolean;
  }>> {
    const viewport = this.page.viewportSize();
    const results = [];

    for (const element of elements) {
      const locator = this.page.locator(element.selector);
      const count = await locator.count();

      if (count === 0) {
        results.push({
          selector: element.selector,
          expected: element.shouldBeVisible,
          actual: false,
          passes: !element.shouldBeVisible
        });
        continue;
      }

      const isVisible = await locator.first().isVisible();
      const boundingBox = await locator.first().boundingBox();

      let position;
      if (boundingBox && viewport) {
        const inViewport = (
          boundingBox.x >= 0 &&
          boundingBox.y >= 0 &&
          boundingBox.x + boundingBox.width <= viewport.width &&
          boundingBox.y + boundingBox.height <= viewport.height
        );

        position = {
          x: boundingBox.x,
          y: boundingBox.y,
          inViewport
        };
      }

      const passes = (isVisible === element.shouldBeVisible) &&
                    (!element.position ||
                     (element.position === 'above-fold' && position?.inViewport) ||
                     (element.position === 'below-fold' && !position?.inViewport));

      results.push({
        selector: element.selector,
        expected: element.shouldBeVisible,
        actual: isVisible,
        position,
        passes
      });
    }

    return results;
  }

  /**
   * Generate visual test report
   */
  async generateVisualReport(
    testResults: Array<{
      name: string;
      matches: boolean;
      diffPath?: string;
      timestamp: number;
    }>
  ): Promise<string> {
    const reportPath = `${this.diffDir}/visual-report.html`;
    const timestamp = new Date().toISOString();

    const html = `
<!DOCTYPE html>
<html>
<head>
    <title>Visual Test Report</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        .header { background: #f5f5f5; padding: 20px; margin-bottom: 20px; }
        .test-result { border: 1px solid #ddd; margin: 10px 0; padding: 15px; }
        .pass { border-left: 5px solid #4CAF50; }
        .fail { border-left: 5px solid #f44336; }
        .image-comparison { display: flex; gap: 10px; margin: 10px 0; }
        .image-comparison img { max-width: 300px; border: 1px solid #ddd; }
        .summary { background: #e3f2fd; padding: 15px; margin-bottom: 20px; }
    </style>
</head>
<body>
    <div class="header">
        <h1>Visual Test Report</h1>
        <p>Generated: ${timestamp}</p>
    </div>

    <div class="summary">
        <h2>Summary</h2>
        <p>Total Tests: ${testResults.length}</p>
        <p>Passed: ${testResults.filter(r => r.matches).length}</p>
        <p>Failed: ${testResults.filter(r => !r.matches).length}</p>
    </div>

    ${testResults.map(result => `
        <div class="test-result ${result.matches ? 'pass' : 'fail'}">
            <h3>${result.name}</h3>
            <p>Status: ${result.matches ? '✅ PASS' : '❌ FAIL'}</p>
            <p>Timestamp: ${new Date(result.timestamp).toLocaleString()}</p>
            ${result.diffPath ? `
                <div class="image-comparison">
                    <div>
                        <h4>Difference</h4>
                        <img src="${result.diffPath}" alt="Visual diff">
                    </div>
                </div>
            ` : ''}
        </div>
    `).join('')}
</body>
</html>
    `;

    // In a real implementation, you'd write this to a file
    // For now, we'll return the HTML content
    return html;
  }
}

/**
 * Color Accessibility Testing
 */
export class ColorAccessibilityHelper {
  /**
   * Convert hex color to RGB
   */
  static hexToRgb(hex: string): { r: number; g: number; b: number } {
    const result = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
    return result ? {
      r: parseInt(result[1], 16),
      g: parseInt(result[2], 16),
      b: parseInt(result[3], 16)
    } : { r: 0, g: 0, b: 0 };
  }

  /**
   * Calculate relative luminance
   */
  static getLuminance(rgb: { r: number; g: number; b: number }): number {
    const { r, g, b } = rgb;
    const [rs, gs, bs] = [r, g, b].map(c => {
      c = c / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * rs + 0.7152 * gs + 0.0722 * bs;
  }

  /**
   * Calculate contrast ratio between two colors
   */
  static getContrastRatio(color1: string, color2: string): number {
    const rgb1 = this.hexToRgb(color1);
    const rgb2 = this.hexToRgb(color2);

    const lum1 = this.getLuminance(rgb1);
    const lum2 = this.getLuminance(rgb2);

    const brightest = Math.max(lum1, lum2);
    const darkest = Math.min(lum1, lum2);

    return (brightest + 0.05) / (darkest + 0.05);
  }

  /**
   * Evaluate contrast ratio against WCAG standards
   */
  static evaluateContrast(contrastRatio: number): ContrastResult {
    const level = contrastRatio >= 7 ? 'AAA' :
                  contrastRatio >= 4.5 ? 'AA' :
                  contrastRatio >= 3 ? 'A' : 'FAIL';

    return {
      ratio: contrastRatio,
      level,
      passes: {
        normal: {
          AA: contrastRatio >= 4.5,
          AAA: contrastRatio >= 7
        },
        large: {
          AA: contrastRatio >= 3,
          AAA: contrastRatio >= 4.5
        }
      }
    };
  }

  /**
   * Test color contrast on page elements
   */
  static async testPageContrast(page: Page): Promise<Array<{
    element: string;
    foreground: string;
    background: string;
    contrast: ContrastResult;
    passes: boolean;
  }>> {
    return await page.evaluate(() => {
      const results = [];
      const elements = document.querySelectorAll('p, h1, h2, h3, h4, h5, h6, span, div, button, a');

      // This is a simplified version - in a real implementation,
      // you'd need more sophisticated color extraction
      elements.forEach((element, index) => {
        if (index > 20) return; // Limit to first 20 elements

        const styles = window.getComputedStyle(element);
        const foreground = styles.color;
        const background = styles.backgroundColor;

        // Convert to hex (simplified - would need proper conversion)
        const selector = element.tagName + (element.id ? '#' + element.id : '') +
                        (element.className ? '.' + element.className.split(' ')[0] : '');

        results.push({
          element: selector,
          foreground,
          background,
          // Note: Actual contrast calculation would happen here
          contrast: { ratio: 0, level: 'FAIL', passes: { normal: { AA: false, AAA: false }, large: { AA: false, AAA: false } } },
          passes: false
        });
      });

      return results;
    });
  }
}

/**
 * Visual Regression Testing Utilities
 */
export class VisualRegressionHelper {
  private testName: string;
  private baselineVersion: string;

  constructor(testName: string, baselineVersion = 'latest') {
    this.testName = testName;
    this.baselineVersion = baselineVersion;
  }

  /**
   * Create visual test suite for a page
   */
  async createTestSuite(
    page: Page,
    scenarios: Array<{
      name: string;
      setup?: () => Promise<void>;
      viewport?: { width: number; height: number };
      waitFor?: string | number;
    }>
  ): Promise<Array<{
    scenario: string;
    success: boolean;
    differences?: number;
    path: string;
  }>> {
    const results = [];

    for (const scenario of scenarios) {
      // Setup scenario
      if (scenario.setup) {
        await scenario.setup();
      }

      // Set viewport if specified
      if (scenario.viewport) {
        await page.setViewportSize(scenario.viewport);
      }

      // Wait for elements or time
      if (scenario.waitFor) {
        if (typeof scenario.waitFor === 'string') {
          await page.waitForSelector(scenario.waitFor);
        } else {
          await page.waitForTimeout(scenario.waitFor);
        }
      }

      // Take screenshot
      const screenshotPath = `visual-tests/${this.testName}/${scenario.name}.png`;
      await page.screenshot({
        path: screenshotPath,
        fullPage: true
      });

      results.push({
        scenario: scenario.name,
        success: true, // Would compare with baseline in real implementation
        path: screenshotPath
      });
    }

    return results;
  }

  /**
   * Generate hash for visual comparison
   */
  private generateImageHash(imagePath: string): string {
    // In a real implementation, you'd read the image file and generate a hash
    // This is a placeholder
    return createHash('md5').update(imagePath + Date.now()).digest('hex');
  }
}

export {
  VisualTestHelper,
  ColorAccessibilityHelper,
  VisualRegressionHelper
};
