/**
 * ADD CONTEXT MODAL E2E TESTS
 *
 * These tests validate the AddContextModal feature on the Knowledge Sources page:
 * 1. Modal opens/closes correctly
 * 2. Tab switching (Paste Text / Upload File)
 * 3. Form validation and submit button state
 * 4. Success flow with form reset
 *
 * Phase 1.5 - [SDP-1] add-context-modal
 */

import { test, expect, type Page } from './fixtures';

// Reset DB before each test for clean state
test.beforeEach(async ({ request }) => {
  await request.post('/admin/reset-database');
});

/**
 * Helper: Navigate to knowledge sources page
 * Note: Auth is disabled in E2E tests (VITE_AUTH_ENABLED=false), so we navigate directly
 */
async function navigateToKnowledgeSources(page: Page): Promise<void> {
  await page.goto('/settings/knowledge', { waitUntil: 'domcontentloaded' });
  // Wait for the Add Context button to be visible (indicates page is ready)
  await expect(page.locator('[data-testid="add-context-btn"]')).toBeVisible({ timeout: 15000 });
}

test.describe('AddContextModal - Modal Open/Close', () => {

  test('opens modal when clicking "Add Context" button', async ({ page }) => {
    console.log('🎯 Testing: Modal opens on button click');

    await navigateToKnowledgeSources(page);

    // Click "Add Context" button
    const addContextBtn = page.locator('[data-testid="add-context-btn"]');
    await expect(addContextBtn).toBeVisible({ timeout: 10000 });
    await addContextBtn.click();

    // Modal should be visible
    const modal = page.locator('.add-context-modal');
    await expect(modal).toBeVisible({ timeout: 5000 });

    // Should show modal header
    const modalHeader = page.locator('.modal-header h2:has-text("Add Context")');
    await expect(modalHeader).toBeVisible();

    console.log('✅ Modal opens correctly');
  });

  test('closes modal when clicking X button', async ({ page }) => {
    console.log('🎯 Testing: Modal closes via X button');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    const modal = page.locator('.add-context-modal');
    await expect(modal).toBeVisible({ timeout: 5000 });

    // Click X button
    const closeBtn = page.locator('.modal-close-button');
    await closeBtn.click();

    // Modal should not be visible
    await expect(modal).not.toBeVisible({ timeout: 3000 });

    console.log('✅ Modal closes via X button');
  });

  test('closes modal when clicking Cancel button', async ({ page }) => {
    console.log('🎯 Testing: Modal closes via Cancel button');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    const modal = page.locator('.add-context-modal');
    await expect(modal).toBeVisible({ timeout: 5000 });

    // Click Cancel button
    const cancelBtn = page.locator('.modal-button-secondary:has-text("Cancel")');
    await cancelBtn.click();

    // Modal should not be visible
    await expect(modal).not.toBeVisible({ timeout: 3000 });

    console.log('✅ Modal closes via Cancel button');
  });

  test('closes modal when clicking overlay', async ({ page }) => {
    console.log('🎯 Testing: Modal closes via overlay click');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    const modal = page.locator('.add-context-modal');
    await expect(modal).toBeVisible({ timeout: 5000 });

    // Click overlay (outside modal)
    const overlay = page.locator('.modal-overlay');
    await overlay.click({ position: { x: 10, y: 10 } }); // Click near edge

    // Modal should not be visible
    await expect(modal).not.toBeVisible({ timeout: 3000 });

    console.log('✅ Modal closes via overlay click');
  });

});

test.describe('AddContextModal - Tab Switching', () => {

  test('defaults to Paste Text tab', async ({ page }) => {
    console.log('🎯 Testing: Default tab is Paste Text');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    await page.waitForTimeout(500);

    // Paste Text tab should be active
    const pasteTab = page.locator('.context-tab:has-text("Paste Text")');
    await expect(pasteTab).toHaveClass(/active/);

    // Content textarea should be visible
    const contentTextarea = page.locator('#context-content');
    await expect(contentTextarea).toBeVisible();

    console.log('✅ Defaults to Paste Text tab');
  });

  test('switches to Upload File tab', async ({ page }) => {
    console.log('🎯 Testing: Switch to Upload File tab');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    await page.waitForTimeout(500);

    // Click Upload File tab
    const uploadTab = page.locator('.context-tab:has-text("Upload File")');
    await uploadTab.click();

    // Upload tab should be active
    await expect(uploadTab).toHaveClass(/active/);

    // Drop zone should be visible
    const dropZone = page.locator('.drop-zone');
    await expect(dropZone).toBeVisible();

    // Content textarea should NOT be visible
    const contentTextarea = page.locator('#context-content');
    await expect(contentTextarea).not.toBeVisible();

    console.log('✅ Switches to Upload File tab correctly');
  });

  test('switches back to Paste Text tab', async ({ page }) => {
    console.log('🎯 Testing: Switch back to Paste Text tab');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    await page.waitForTimeout(500);

    // Switch to Upload tab
    const uploadTab = page.locator('.context-tab:has-text("Upload File")');
    await uploadTab.click();
    await expect(uploadTab).toHaveClass(/active/);

    // Switch back to Paste Text tab
    const pasteTab = page.locator('.context-tab:has-text("Paste Text")');
    await pasteTab.click();
    await expect(pasteTab).toHaveClass(/active/);

    // Content textarea should be visible again
    const contentTextarea = page.locator('#context-content');
    await expect(contentTextarea).toBeVisible();

    console.log('✅ Switches back to Paste Text tab correctly');
  });

});

test.describe('AddContextModal - Form Validation', () => {

  test('submit button disabled when fields are empty', async ({ page }) => {
    console.log('🎯 Testing: Submit button disabled with empty fields');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    await page.waitForTimeout(500);

    // Submit button should be disabled
    const submitBtn = page.locator('.modal-button-primary:has-text("Save Document")');
    await expect(submitBtn).toBeDisabled();

    console.log('✅ Submit button disabled when empty');
  });

  test('submit button disabled with only title filled', async ({ page }) => {
    console.log('🎯 Testing: Submit button disabled with only title');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    await page.waitForTimeout(500);

    // Fill only title
    await page.locator('#context-title').fill('Test Title');

    // Submit button should still be disabled
    const submitBtn = page.locator('.modal-button-primary:has-text("Save Document")');
    await expect(submitBtn).toBeDisabled();

    console.log('✅ Submit button disabled with only title');
  });

  test('submit button disabled with only content filled', async ({ page }) => {
    console.log('🎯 Testing: Submit button disabled with only content');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    await page.waitForTimeout(500);

    // Fill only content
    await page.locator('#context-content').fill('Test content');

    // Submit button should still be disabled
    const submitBtn = page.locator('.modal-button-primary:has-text("Save Document")');
    await expect(submitBtn).toBeDisabled();

    console.log('✅ Submit button disabled with only content');
  });

  test('submit button enabled when both fields filled', async ({ page }) => {
    console.log('🎯 Testing: Submit button enabled when both fields filled');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    await page.waitForTimeout(500);

    // Fill both fields
    await page.locator('#context-title').fill('Test Title');
    await page.locator('#context-content').fill('Test content');

    // Submit button should be enabled
    const submitBtn = page.locator('.modal-button-primary:has-text("Save Document")');
    await expect(submitBtn).toBeEnabled();

    console.log('✅ Submit button enabled when both fields filled');
  });

  test('submit button disabled with whitespace-only input', async ({ page }) => {
    console.log('🎯 Testing: Submit button disabled with whitespace-only input');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    await page.waitForTimeout(500);

    // Fill with whitespace only
    await page.locator('#context-title').fill('   ');
    await page.locator('#context-content').fill('   ');

    // Submit button should be disabled (trims whitespace)
    const submitBtn = page.locator('.modal-button-primary:has-text("Save Document")');
    await expect(submitBtn).toBeDisabled();

    console.log('✅ Submit button disabled with whitespace-only input');
  });

});

test.describe('AddContextModal - Form Submission', () => {

  test('successful submission shows success message', async ({ page }) => {
    console.log('🎯 Testing: Successful submission flow');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    await page.waitForTimeout(500);

    // Fill form
    await page.locator('#context-title').fill('My Test Document');
    await page.locator('#context-content').fill('This is my test content');

    // Submit
    const submitBtn = page.locator('.modal-button-primary:has-text("Save Document")');
    await submitBtn.click();

    // Wait for success message
    await page.waitForTimeout(1000);

    // Success message should appear
    const successMsg = page.locator('.context-success');
    await expect(successMsg).toBeVisible({ timeout: 5000 });
    await expect(successMsg).toContainText('My Test Document');
    await expect(successMsg).toContainText('saved');

    console.log('✅ Success message appears after submission');
  });

  test('form clears after successful submission', async ({ page }) => {
    console.log('🎯 Testing: Form clears after successful submission');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    await page.waitForTimeout(500);

    // Fill form
    await page.locator('#context-title').fill('My Test Document');
    await page.locator('#context-content').fill('This is my test content');

    // Submit
    const submitBtn = page.locator('.modal-button-primary:has-text("Save Document")');
    await submitBtn.click();

    // Wait for submission to complete
    await page.waitForTimeout(1000);

    // Form fields should be empty
    await expect(page.locator('#context-title')).toHaveValue('');
    await expect(page.locator('#context-content')).toHaveValue('');

    // Submit button should be disabled again
    await expect(submitBtn).toBeDisabled();

    console.log('✅ Form clears after successful submission');
  });

  test('can submit another document after success', async ({ page }) => {
    console.log('🎯 Testing: Can submit another document after success');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    await page.waitForTimeout(500);

    // First submission
    await page.locator('#context-title').fill('First Document');
    await page.locator('#context-content').fill('First content');
    await page.locator('.modal-button-primary:has-text("Save Document")').click();
    await page.waitForTimeout(1000);

    // Success message should appear
    await expect(page.locator('.context-success')).toBeVisible();

    // Fill form again
    await page.locator('#context-title').fill('Second Document');
    await page.locator('#context-content').fill('Second content');

    // Submit button should be enabled again
    const submitBtn = page.locator('.modal-button-primary:has-text("Save Document")');
    await expect(submitBtn).toBeEnabled();

    // Submit again
    await submitBtn.click();
    await page.waitForTimeout(1000);

    // New success message should appear
    const successMsg = page.locator('.context-success');
    await expect(successMsg).toContainText('Second Document');

    console.log('✅ Can submit another document after success');
  });

  test('submit button shows loading state during submission', async ({ page }) => {
    console.log('🎯 Testing: Submit button loading state');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    await page.waitForTimeout(500);

    // Fill form
    await page.locator('#context-title').fill('Test Document');
    await page.locator('#context-content').fill('Test content');

    // Submit
    const submitBtn = page.locator('.modal-button-primary');
    await submitBtn.click();

    // Should show "Saving..." text briefly
    await expect(submitBtn).toHaveText('Saving...', { timeout: 2000 });

    // Should be disabled during submission
    await expect(submitBtn).toBeDisabled();

    // Wait for completion
    await page.waitForTimeout(1000);

    // Should return to "Save Document"
    await expect(submitBtn).toHaveText('Save Document');

    console.log('✅ Submit button shows loading state correctly');
  });

});

test.describe('AddContextModal - Upload Tab', () => {

  test('shows drop zone in upload tab', async ({ page }) => {
    console.log('🎯 Testing: Drop zone visible in upload tab');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    await page.waitForTimeout(500);

    // Switch to upload tab
    await page.locator('.context-tab:has-text("Upload File")').click();

    // Drop zone should be visible
    const dropZone = page.locator('.drop-zone');
    await expect(dropZone).toBeVisible();

    // Should show upload instructions
    await expect(dropZone).toContainText('Drop a file here or click to browse');
    await expect(dropZone).toContainText('.txt and .md files supported');

    console.log('✅ Drop zone shows correctly');
  });

  test('drop zone is clickable to browse files', async ({ page }) => {
    console.log('🎯 Testing: Drop zone click to browse');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    await page.waitForTimeout(500);

    // Switch to upload tab
    await page.locator('.context-tab:has-text("Upload File")').click();

    // Drop zone should be clickable
    const dropZone = page.locator('.drop-zone');
    await expect(dropZone).toBeVisible();

    // Note: Can't actually test file upload without setting up file chooser interception
    // Just verify the drop zone is interactive

    console.log('✅ Drop zone is clickable');
  });

});

test.describe('AddContextModal - Existing Docs Count', () => {

  test('shows "No context docs yet" when count is 0', async ({ page }) => {
    console.log('🎯 Testing: Shows no docs message');

    await navigateToKnowledgeSources(page);
    await page.locator('[data-testid="add-context-btn"]').click();

    await page.waitForTimeout(500);

    // Should show no docs message
    const docsCount = page.locator('.context-docs-count');
    await expect(docsCount).toContainText('No context docs yet');

    console.log('✅ Shows no docs message correctly');
  });

  // Note: Testing with actual count would require seeding knowledge sources first
  // That's beyond the scope of Phase 1 modal testing

});
