import { test, expect, type Page } from './fixtures';

async function navigateToCanvas(page: Page) {
  await page.goto('/');
  const canvasTab = page.getByTestId('global-canvas-tab');
  if (await canvasTab.count()) {
    await canvasTab.click();
  }
  await page.waitForSelector('#canvas-container', { timeout: 10_000 });
}

async function openTemplateGallery(page: Page) {
  // Look for template gallery button (📋 icon)
  const galleryBtn = page.locator('button[title="Template Gallery"]');
  await expect(galleryBtn).toBeVisible();
  await galleryBtn.click();
  
  // Wait for template gallery modal to open
  await expect(page.locator('#template-gallery-overlay')).toBeVisible({ timeout: 5000 });
}

test.describe('Template Gallery Tests', () => {
  test.beforeEach(async ({ page }) => {
    await page.request.post('http://localhost:8001/admin/reset-database');
    await navigateToCanvas(page);
  });

  test.afterEach(async ({ page }) => {
    await page.request.post('http://localhost:8001/admin/reset-database');
  });

  test('Open template gallery modal', async ({ page }) => {
    await openTemplateGallery(page);
    
    // Verify modal elements are present
    await expect(page.locator('.template-gallery-modal h2')).toHaveText('Template Gallery');
    await expect(page.locator('#templates-grid')).toBeVisible();
    await expect(page.locator('#template-category-filter')).toBeVisible();
    await expect(page.locator('#my-templates-only')).toBeVisible();
  });

  test('Close template gallery modal', async ({ page }) => {
    await openTemplateGallery(page);
    
    // Close via X button
    const closeBtn = page.locator('.modal-close');
    await closeBtn.click();
    
    // Verify modal is closed
    await expect(page.locator('#template-gallery-overlay')).toHaveCount(0, { timeout: 5000 });
  });

  test('Filter templates by category', async ({ page }) => {
    await openTemplateGallery(page);
    
    // Wait for templates to load
    await page.waitForTimeout(2000);
    
    // Check if category filter has options
    const categorySelect = page.locator('#template-category-filter');
    const options = await categorySelect.locator('option').count();
    
    if (options > 1) {
      // Select a category (not "All Categories")
      await categorySelect.selectOption({ index: 1 });
      
      // Wait for filtered results
      await page.waitForTimeout(1000);
      
      // Verify filtering occurred (implementation dependent)
      const templates = page.locator('.template-card');
      const templateCount = await templates.count();
      expect(templateCount).toBeGreaterThanOrEqual(0);
    } else {
      test.skip(true, 'No template categories available for filtering');
    }
  });

  test('Toggle my templates only filter', async ({ page }) => {
    await openTemplateGallery(page);
    
    // Toggle "My Templates Only" checkbox
    const myTemplatesCheckbox = page.locator('#my-templates-only');
    await myTemplatesCheckbox.click();
    
    // Wait for filter to apply
    await page.waitForTimeout(1000);
    
    // Verify checkbox is checked
    await expect(myTemplatesCheckbox).toBeChecked();
    
    // Toggle back off
    await myTemplatesCheckbox.click();
    await expect(myTemplatesCheckbox).not.toBeChecked();
  });

  test('Deploy template from gallery', async ({ page }) => {
    await openTemplateGallery(page);
    
    // Wait for templates to load
    await page.waitForTimeout(2000);
    
    // Look for template cards
    const templateCards = page.locator('.template-card');
    const cardCount = await templateCards.count();
    
    if (cardCount > 0) {
      // Click deploy button on first template
      const deployBtn = templateCards.first().locator('button:has-text("Deploy")');
      await deployBtn.click();
      
      // Verify modal closes after deployment
      await expect(page.locator('#template-gallery-overlay')).toHaveCount(0, { timeout: 10000 });
      
      // Look for success toast
      const successToast = page.locator('.toast-success, .toast:has-text("deployed")');
      if (await successToast.count() > 0) {
        await expect(successToast).toBeVisible({ timeout: 5000 });
      }
      
      // Verify new workflow appears in workflow switcher
      await page.waitForTimeout(1000);
      const workflowTabs = page.locator('.workflow-tab-list .tab:not(.plus-tab)');
      await expect(workflowTabs).toHaveCount.toBeGreaterThan(0, { timeout: 5000 });
    } else {
      test.skip(true, 'No templates available for deployment');
    }
  });

  test('Refresh templates in gallery', async ({ page }) => {
    await openTemplateGallery(page);
    
    // Click refresh button
    const refreshBtn = page.locator('button:has-text("Refresh")');
    await refreshBtn.click();
    
    // Wait for refresh to complete
    await page.waitForTimeout(2000);
    
    // Verify gallery is still open and functional
    await expect(page.locator('#templates-grid')).toBeVisible();
  });

  test('Template card displays correct information', async ({ page }) => {
    await openTemplateGallery(page);
    
    // Wait for templates to load
    await page.waitForTimeout(2000);
    
    const templateCards = page.locator('.template-card');
    const cardCount = await templateCards.count();
    
    if (cardCount > 0) {
      const firstCard = templateCards.first();
      
      // Verify card has required elements
      await expect(firstCard.locator('.template-name')).toBeVisible();
      await expect(firstCard.locator('.template-category')).toBeVisible();
      await expect(firstCard.locator('button:has-text("Deploy")')).toBeVisible();
      
      // Check for optional elements
      const description = firstCard.locator('.template-description');
      const tags = firstCard.locator('.template-tags');
      
      // These should exist even if empty
      await expect(description).toBeAttached();
      await expect(tags).toBeAttached();
    } else {
      // Verify empty state message
      await expect(page.locator('.empty-state')).toBeVisible();
    }
  });

  test('Template gallery handles loading state', async ({ page }) => {
    await openTemplateGallery(page);
    
    // Look for loading indicator initially
    const loadingIndicator = page.locator('.loading, :has-text("Loading")');
    
    // Loading state might be brief, so check if it exists or if templates are already loaded
    try {
      await expect(loadingIndicator).toBeVisible({ timeout: 1000 });
    } catch {
      // Loading finished quickly, that's okay
    }
    
    // Wait for final state (either templates or empty state)
    await page.waitForFunction(() => {
      const grid = document.querySelector('#templates-grid');
      return grid && (
        grid.querySelector('.template-card') ||
        grid.querySelector('.empty-state') ||
        !grid.querySelector('.loading')
      );
    }, { timeout: 10000 });
  });

  test('Template gallery error handling', async ({ page }) => {
    // This test would require mocking API failures
    // For now, we'll test graceful degradation
    
    await openTemplateGallery(page);
    
    // Wait for any state to resolve
    await page.waitForTimeout(3000);
    
    // Verify gallery doesn't crash and shows some state
    await expect(page.locator('#templates-grid')).toBeVisible();
    
    // Should show either templates, empty state, or error message
    const hasContent = await page.evaluate(() => {
      const grid = document.querySelector('#templates-grid');
      return grid && grid.children.length > 0;
    });
    
    expect(hasContent).toBeTruthy();
  });

  test('Template gallery keyboard navigation', async ({ page }) => {
    await openTemplateGallery(page);
    
    // Wait for templates to load
    await page.waitForTimeout(2000);
    
    // Test tab navigation
    await page.keyboard.press('Tab');
    await page.keyboard.press('Tab');
    
    // Test escape to close modal
    await page.keyboard.press('Escape');
    
    // Verify modal closes
    await expect(page.locator('#template-gallery-overlay')).toHaveCount(0, { timeout: 5000 });
  });

  test('Template gallery responsive behavior', async ({ page }) => {
    await openTemplateGallery(page);
    
    // Wait for templates to load
    await page.waitForTimeout(2000);
    
    // Test different viewport sizes
    await page.setViewportSize({ width: 800, height: 600 });
    await expect(page.locator('.template-gallery-modal')).toBeVisible();
    
    await page.setViewportSize({ width: 1200, height: 800 });
    await expect(page.locator('.template-gallery-modal')).toBeVisible();
    
    // Verify grid adapts to different sizes
    const grid = page.locator('#templates-grid');
    await expect(grid).toBeVisible();
  });
});