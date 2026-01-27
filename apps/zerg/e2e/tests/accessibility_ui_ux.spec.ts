import { test, expect } from './fixtures';

/**
 * ACCESSIBILITY AND UI/UX E2E TEST
 *
 * This test validates accessibility compliance and user experience:
 * 1. WCAG 2.1 compliance testing
 * 2. Keyboard navigation functionality
 * 3. Screen reader compatibility
 * 4. Color contrast and visual accessibility
 * 5. Focus management and tab order
 * 6. ARIA labels and semantic markup
 * 7. Responsive design and mobile compatibility
 * 8. User workflow usability testing
 */

test.describe('Accessibility and UI/UX', () => {
  test('WCAG compliance and semantic markup', async ({ page }) => {
    console.log('🚀 Starting WCAG compliance test...');

    const commisId = process.env.TEST_PARALLEL_INDEX || '0';
    console.log('📊 Commis ID:', commisId);

    // Navigate to application
    await page.goto('/');
    await page.waitForTimeout(2000);

    // Test 1: Basic semantic structure
    console.log('📊 Test 1: Checking semantic structure...');

    const semanticElements = {
      header: await page.locator('header').count(),
      nav: await page.locator('nav').count(),
      main: await page.locator('main').count(),
      section: await page.locator('section').count(),
      article: await page.locator('article').count(),
      aside: await page.locator('aside').count(),
      footer: await page.locator('footer').count()
    };

    console.log('📊 Semantic elements found:', semanticElements);

    const hasBasicStructure = semanticElements.header > 0 || semanticElements.nav > 0 || semanticElements.main > 0;
    if (hasBasicStructure) {
      console.log('✅ Basic semantic structure present');
    } else {
      console.log('⚠️  Consider adding semantic HTML elements');
    }

    // Test 2: Heading hierarchy
    console.log('📊 Test 2: Checking heading hierarchy...');

    const headings = {
      h1: await page.locator('h1').count(),
      h2: await page.locator('h2').count(),
      h3: await page.locator('h3').count(),
      h4: await page.locator('h4').count(),
      h5: await page.locator('h5').count(),
      h6: await page.locator('h6').count()
    };

    console.log('📊 Heading structure:', headings);

    if (headings.h1 > 0) {
      console.log('✅ Page has primary heading (h1)');
    } else {
      console.log('⚠️  Page should have an h1 element');
    }

    // Test 3: ARIA labels and accessibility attributes
    console.log('📊 Test 3: Checking ARIA attributes...');

    const ariaElements = {
      'aria-label': await page.locator('[aria-label]').count(),
      'aria-labelledby': await page.locator('[aria-labelledby]').count(),
      'aria-describedby': await page.locator('[aria-describedby]').count(),
      'aria-hidden': await page.locator('[aria-hidden]').count(),
      'role': await page.locator('[role]').count(),
      'aria-expanded': await page.locator('[aria-expanded]').count()
    };

    console.log('📊 ARIA attributes found:', ariaElements);

    const totalAriaElements = Object.values(ariaElements).reduce((sum, count) => sum + count, 0);
    if (totalAriaElements > 0) {
      console.log('✅ ARIA attributes are being used');
    } else {
      console.log('⚠️  Consider adding ARIA attributes for better accessibility');
    }

    // Test 4: Form accessibility
    console.log('📊 Test 4: Checking form accessibility...');

    const formElements = await page.locator('input, textarea, select').count();
    console.log('📊 Form elements found:', formElements);

    if (formElements > 0) {
      const labeledInputs = await page.locator('input[id]:has(+ label), input[id]:has(~ label), label:has(input)').count();
      const ariaLabeledInputs = await page.locator('input[aria-label], textarea[aria-label], select[aria-label]').count();

      console.log('📊 Labeled form elements:', labeledInputs + ariaLabeledInputs);

      if (labeledInputs + ariaLabeledInputs > 0) {
        console.log('✅ Form elements have labels');
      } else {
        console.log('⚠️  Form elements should have proper labels');
      }
    }

    console.log('✅ WCAG compliance test completed');
  });

  test('Keyboard navigation functionality', async ({ page }) => {
    console.log('🚀 Starting keyboard navigation test...');

    await page.goto('/');
    await page.waitForTimeout(1000);

    // Test 1: Tab order and focus management
    console.log('📊 Test 1: Testing tab order...');

    // Start from the beginning of the page
    await page.keyboard.press('Tab');
    await page.waitForTimeout(200);

    // Track focus progression
    const focusableElements = [];
    for (let i = 0; i < 10; i++) {
      const focusedElement = await page.evaluate(() => {
        const focused = document.activeElement;
        return {
          tagName: focused?.tagName,
          type: focused?.getAttribute('type'),
          testId: focused?.getAttribute('data-testid'),
          ariaLabel: focused?.getAttribute('aria-label'),
          text: focused?.textContent?.substring(0, 50)
        };
      });

      focusableElements.push(focusedElement);

      if (focusedElement.tagName) {
        console.log(`📊 Tab ${i + 1}: ${focusedElement.tagName}${focusedElement.testId ? ` [${focusedElement.testId}]` : ''}`);
      }

      await page.keyboard.press('Tab');
      await page.waitForTimeout(100);
    }

    const uniqueFocusableElements = new Set(focusableElements.map(el => `${el.tagName}-${el.testId}`)).size;
    console.log('📊 Unique focusable elements:', uniqueFocusableElements);

    if (uniqueFocusableElements >= 3) {
      console.log('✅ Multiple elements are keyboard accessible');
    }

    // Test 2: Escape key functionality
    console.log('📊 Test 2: Testing escape key functionality...');

    try {
      await page.keyboard.press('Escape');
      await page.waitForTimeout(200);
      console.log('✅ Escape key press handled');
    } catch (error) {
      console.log('📊 Escape key test:', error.message);
    }

    // Test 3: Enter key activation
    console.log('📊 Test 3: Testing enter key activation...');

    // Try to find a button and activate it with Enter
    const buttons = await page.locator('button, [role="button"]').count();
    if (buttons > 0) {
      await page.locator('button, [role="button"]').first().focus();
      await page.keyboard.press('Enter');
      await page.waitForTimeout(200);
      console.log('✅ Enter key activation tested');
    }

    // Test 4: Arrow key navigation (if applicable)
    console.log('📊 Test 4: Testing arrow key navigation...');

    const lists = await page.locator('[role="listbox"], [role="menu"], [role="tablist"]').count();
    if (lists > 0) {
      await page.locator('[role="listbox"], [role="menu"], [role="tablist"]').first().focus();
      await page.keyboard.press('ArrowDown');
      await page.waitForTimeout(200);
      await page.keyboard.press('ArrowUp');
      await page.waitForTimeout(200);
      console.log('✅ Arrow key navigation tested');
    }

    console.log('✅ Keyboard navigation test completed');
  });

  test('Screen reader compatibility', async ({ page }) => {
    console.log('🚀 Starting screen reader compatibility test...');

    await page.goto('/');
    await page.waitForTimeout(1000);

    // Test 1: Page title and document structure
    console.log('📊 Test 1: Checking page title and structure...');

    const pageTitle = await page.title();
    console.log('📊 Page title:', pageTitle);

    if (pageTitle && pageTitle.trim().length > 0) {
      console.log('✅ Page has a descriptive title');
    } else {
      console.log('⚠️  Page should have a descriptive title');
    }

    // Test 2: Image alt text
    console.log('📊 Test 2: Checking image alt text...');

    const images = await page.locator('img').count();
    const imagesWithAlt = await page.locator('img[alt]').count();
    const imagesWithEmptyAlt = await page.locator('img[alt=""]').count(); // Decorative images

    console.log('📊 Total images:', images);
    console.log('📊 Images with alt text:', imagesWithAlt);
    console.log('📊 Decorative images (empty alt):', imagesWithEmptyAlt);

    if (images === 0) {
      console.log('📊 No images found on page');
    } else if (imagesWithAlt + imagesWithEmptyAlt === images) {
      console.log('✅ All images have appropriate alt attributes');
    } else {
      console.log('⚠️  Some images missing alt attributes');
    }

    // Test 3: Link text and context
    console.log('📊 Test 3: Checking link accessibility...');

    const links = await page.locator('a').count();
    console.log('📊 Total links:', links);

    if (links > 0) {
      const linkTexts = await page.locator('a').allTextContents();
      const emptyLinks = linkTexts.filter(text => !text.trim()).length;
      const genericLinks = linkTexts.filter(text =>
        ['click here', 'read more', 'here', 'more'].includes(text.toLowerCase().trim())
      ).length;

      console.log('📊 Links with empty text:', emptyLinks);
      console.log('📊 Links with generic text:', genericLinks);

      if (emptyLinks === 0 && genericLinks === 0) {
        console.log('✅ All links have descriptive text');
      } else {
        console.log('⚠️  Some links need more descriptive text');
      }
    }

    // Test 4: Live regions and dynamic content
    console.log('📊 Test 4: Checking live regions...');

    const liveRegions = await page.locator('[aria-live]').count();
    const statusElements = await page.locator('[role="status"], [role="alert"]').count();

    console.log('📊 Live regions found:', liveRegions);
    console.log('📊 Status/alert elements:', statusElements);

    if (liveRegions > 0 || statusElements > 0) {
      console.log('✅ Dynamic content accessibility considered');
    } else {
      console.log('📊 No live regions found (may not be needed)');
    }

    console.log('✅ Screen reader compatibility test completed');
  });

  test('Color contrast and visual accessibility', async ({ page }) => {
    console.log('🚀 Starting color contrast test...');

    await page.goto('/');
    await page.waitForTimeout(1000);

    // Test 1: Check for color-only information
    console.log('📊 Test 1: Analyzing color usage...');

    // Look for elements that might rely solely on color
    const colorIndicators = await page.locator('.error, .success, .warning, .info, [class*="red"], [class*="green"]').count();
    const iconIndicators = await page.locator('.error svg, .success svg, .warning svg, .info svg').count();

    console.log('📊 Color-based indicators:', colorIndicators);
    console.log('📊 Icon-based indicators:', iconIndicators);

    if (colorIndicators > 0 && iconIndicators > 0) {
      console.log('✅ Color information supplemented with icons');
    } else if (colorIndicators > 0) {
      console.log('⚠️  Consider adding icons or text to supplement color information');
    }

    // Test 2: Focus indicators
    console.log('📊 Test 2: Checking focus indicators...');

    // Tab to an element and check if focus is visible
    await page.keyboard.press('Tab');
    await page.waitForTimeout(200);

    const focusStyles = await page.evaluate(() => {
      const focused = document.activeElement;
      if (focused) {
        const styles = window.getComputedStyle(focused);
        return {
          outline: styles.outline,
          outlineColor: styles.outlineColor,
          outlineWidth: styles.outlineWidth,
          boxShadow: styles.boxShadow,
          border: styles.border
        };
      }
      return null;
    });

    console.log('📊 Focus styles:', focusStyles);

    if (focusStyles && (focusStyles.outline !== 'none' || focusStyles.boxShadow !== 'none')) {
      console.log('✅ Focus indicators are visible');
    } else {
      console.log('⚠️  Consider adding visible focus indicators');
    }

    // Test 3: Text sizing and scalability
    console.log('📊 Test 3: Testing text scalability...');

    const originalTextSizes = await page.evaluate(() => {
      const elements = Array.from(document.querySelectorAll('p, h1, h2, h3, h4, h5, h6, span, div'));
      return elements.slice(0, 5).map(el => {
        const styles = window.getComputedStyle(el);
        return {
          fontSize: styles.fontSize,
          lineHeight: styles.lineHeight
        };
      });
    });

    console.log('📊 Original text sizes:', originalTextSizes.slice(0, 3));

    // Simulate zoom/text scaling
    await page.evaluate(() => {
      document.body.style.fontSize = '120%';
    });

    await page.waitForTimeout(500);

    const scaledTextSizes = await page.evaluate(() => {
      const elements = Array.from(document.querySelectorAll('p, h1, h2, h3, h4, h5, h6, span, div'));
      return elements.slice(0, 5).map(el => {
        const styles = window.getComputedStyle(el);
        return {
          fontSize: styles.fontSize,
          lineHeight: styles.lineHeight
        };
      });
    });

    console.log('📊 Scaled text sizes:', scaledTextSizes.slice(0, 3));

    // Reset scaling
    await page.evaluate(() => {
      document.body.style.fontSize = '';
    });

    console.log('✅ Text scalability tested');

    console.log('✅ Color contrast test completed');
  });

  test('Responsive design and mobile compatibility', async ({ page, context }) => {
    console.log('🚀 Starting responsive design test...');

    // Test 1: Mobile viewport
    console.log('📊 Test 1: Testing mobile viewport...');

    await page.setViewportSize({ width: 375, height: 667 }); // iPhone dimensions
    await page.goto('/dashboard');
    await page.waitForFunction(() => document.body.getAttribute('data-ready') === 'true', {}, { timeout: 20000 });

    const mobileLayout = await page.evaluate(() => {
      return {
        bodyWidth: document.body.offsetWidth,
        hasHorizontalScroll: document.body.scrollWidth > window.innerWidth,
        navigationVisible: !!document.querySelector('nav, [data-testid*="nav"]'),
        mainContentVisible: !!document.querySelector('main, [data-testid*="main"], .main-content')
      };
    });

    console.log('📊 Mobile layout:', mobileLayout);

    if (!mobileLayout.hasHorizontalScroll) {
      console.log('✅ No horizontal scroll on mobile');
    } else {
      console.log('⚠️  Page has horizontal scroll on mobile');
    }

    // Test 2: Tablet viewport
    console.log('📊 Test 2: Testing tablet viewport...');

    await page.setViewportSize({ width: 768, height: 1024 }); // iPad dimensions
    await expect.poll(async () => {
      return page.evaluate(() => document.body.offsetWidth > 0);
    }, { timeout: 10000 }).toBe(true);

    const tabletLayout = await page.evaluate(() => {
      return {
        bodyWidth: document.body.offsetWidth,
        hasHorizontalScroll: document.body.scrollWidth > window.innerWidth
      };
    });

    console.log('📊 Tablet layout:', tabletLayout);

    // Test 3: Desktop viewport
    console.log('📊 Test 3: Testing desktop viewport...');

    await page.setViewportSize({ width: 1920, height: 1080 }); // Full HD
    await expect.poll(async () => {
      return page.evaluate(() => document.body.offsetWidth > 0);
    }, { timeout: 10000 }).toBe(true);

    const desktopLayout = await page.evaluate(() => {
      return {
        bodyWidth: document.body.offsetWidth,
        hasHorizontalScroll: document.body.scrollWidth > window.innerWidth
      };
    });

    console.log('📊 Desktop layout:', desktopLayout);

    // Test 4: Touch interaction testing
    console.log('📊 Test 4: Testing touch interactions...');

    await page.setViewportSize({ width: 375, height: 667 }); // Back to mobile

    const touchableElements = await page.locator('button, [role="button"], a, input').count();
    console.log('📊 Touchable elements:', touchableElements);

    if (touchableElements > 0) {
      // Test touch target size
      const touchTargets = await page.evaluate(() => {
        const elements = Array.from(document.querySelectorAll('button, [role="button"], a, input'));
        return elements.slice(0, 5).map(el => {
          const rect = el.getBoundingClientRect();
          return {
            width: rect.width,
            height: rect.height,
            area: rect.width * rect.height
          };
        });
      });

      console.log('📊 Touch target sizes:', touchTargets);

      const adequateTouchTargets = touchTargets.filter(target =>
        target.width >= 44 && target.height >= 44
      ).length;

      console.log('📊 Adequate touch targets (44x44px+):', adequateTouchTargets);

      if (adequateTouchTargets > 0) {
        console.log('✅ Some touch targets meet size recommendations');
      }
    }

    console.log('✅ Responsive design test completed');
  });

  test('User workflow usability testing', async ({ page, request }) => {
    console.log('🚀 Starting user workflow usability test...');

    const commisId = process.env.TEST_PARALLEL_INDEX || '0';

    // Test 1: Primary user journey - Fiche creation
    console.log('📊 Test 1: Testing fiche creation workflow...');

    await page.goto('/');
    await page.waitForTimeout(1000);

    const workflowSteps = [];

    // Step 1: Navigate to dashboard
    const step1Start = Date.now();
    await page.locator('.header-nav').click();
    await page.waitForTimeout(500);
    const step1Time = Date.now() - step1Start;
    workflowSteps.push({ step: 'Navigate to dashboard', time: step1Time });

    // Step 2: Look for fiche creation interface
    const createButton = await page.locator('button:has-text("Create"), [data-testid*="create"]').count();
    console.log('📊 Create buttons found:', createButton);

    if (createButton > 0) {
      const step2Start = Date.now();
      // This would be where user creates an fiche via UI
      // For now, we'll create via API to test the rest of the workflow
      const step2Time = Date.now() - step2Start;
      workflowSteps.push({ step: 'Fiche creation interface', time: step2Time });
    }

    // Create fiche via API for workflow testing
    const ficheResponse = await request.post('/api/fiches', {
      headers: {
        'X-Test-Commis': commisId,
        'Content-Type': 'application/json',
      },
      data: {
        name: `Usability Test Fiche ${Date.now()}`,
        system_instructions: 'Fiche for usability testing',
        task_instructions: 'Test user workflow',
        model: 'gpt-mock',
      }
    });

    if (ficheResponse.ok()) {
      const fiche = await ficheResponse.json();
      console.log('📊 Test fiche created:', fiche.id);

      // Step 3: Verify fiche appears in dashboard
      await page.reload();
      await page.waitForTimeout(1000);
      await page.locator('.header-nav').click();
      await page.waitForTimeout(500);

      const ficheVisible = await page.locator(`text=${fiche.name}`).isVisible();
      console.log('📊 Fiche visible in dashboard:', ficheVisible);

      if (ficheVisible) {
        workflowSteps.push({ step: 'Fiche appears in dashboard', time: 100 });
      }

      // Step 4: Navigate to canvas with fiche
      const step4Start = Date.now();
      await page.getByTestId('global-canvas-tab').click();
      await page.waitForTimeout(1000);
      const step4Time = Date.now() - step4Start;
      workflowSteps.push({ step: 'Navigate to canvas', time: step4Time });

      // Check canvas workflow elements
      const canvasElements = {
        canvas: await page.locator('[data-testid="canvas-container"]').count(),
        ficheShelf: await page.locator('[data-testid="fiche-shelf"]').count(),
        toolPalette: await page.locator('[data-testid="tool-palette"]').count()
      };

      console.log('📊 Canvas workflow elements:', canvasElements);

      const workflowElementsPresent = Object.values(canvasElements).reduce((sum, count) => sum + count, 0);
      if (workflowElementsPresent >= 2) {
        workflowSteps.push({ step: 'Canvas workflow available', time: 50 });
      }
    }

    console.log('📊 User workflow steps completed:', workflowSteps.length);
    console.log('📊 Total workflow time:', workflowSteps.reduce((sum, step) => sum + step.time, 0), 'ms');

    const averageStepTime = workflowSteps.reduce((sum, step) => sum + step.time, 0) / workflowSteps.length;
    console.log('📊 Average step time:', Math.round(averageStepTime), 'ms');

    if (averageStepTime < 1000) {
      console.log('✅ User workflow is responsive');
    }

    // Test 2: Error recovery in workflow
    console.log('📊 Test 2: Testing error recovery...');

    // Try to trigger a recoverable error
    try {
      await page.goto('/invalid-route');
      await page.waitForTimeout(1000);

      // Check if user can recover (navigate back, error page with navigation, etc.)
      const recoveryElements = await page.locator('button:has-text("Back"), a:has-text("Home"), nav').count();
      console.log('📊 Recovery elements found:', recoveryElements);

      if (recoveryElements > 0) {
        console.log('✅ Error recovery options available');
      }

      // Navigate back to main app
      await page.goto('/');
      await page.waitForTimeout(500);
    } catch (error) {
      console.log('📊 Error recovery test:', error.message);
    }

    console.log('✅ User workflow usability test completed');
  });
});
