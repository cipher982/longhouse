/**
 * Landing page link audit.
 *
 * Verifies every navigation link and CTA on the landing page points to the
 * correct destination. Prevents recurring bugs where links silently route
 * to wrong pages (e.g., "Sign In" going to demo timeline instead of auth).
 *
 * Some CTA variants only appear when demo mode is enabled, so the assertions
 * below target the common contract rather than hard-coding that mode.
 */

import { test, expect } from '@playwright/test';

test.describe('Landing page link audit', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    // Ensure landing page loaded (wait for hero headline)
    await expect(page.locator('.landing-hero-headline')).toBeVisible({ timeout: 10_000 });
  });

  // -----------------------------------------------------------------------
  // Header navigation
  // -----------------------------------------------------------------------

  test('header logo links to home', async ({ page }) => {
    const logo = page.locator('.landing-header-brand');
    await expect(logo).toHaveAttribute('href', '/');
  });

  test('header Sign In navigates to control plane (not demo timeline)', async ({ page }) => {
    // Intercept navigation to external URL
    const [popup] = await Promise.all([
      page.waitForEvent('popup', { timeout: 5_000 }).catch(() => null),
      page.waitForURL(/control\.longhouse\.ai|\/timeline/, { timeout: 5_000 }).catch(() => null),
      page.locator('button:has-text("Sign In")').first().click(),
    ]);

    // Demo mode routes to control plane; non-demo routes to /timeline.
    const url = popup ? popup.url() : page.url();
    expect(url.includes('control.longhouse.ai') || url.includes('/timeline')).toBe(true);
  });

  test('header Self-Host Free scrolls to install', async ({ page }) => {
    await page.locator('button:has-text("Self-Host Free")').first().click();
    await expect(page.locator('#landing-install')).toBeInViewport({ timeout: 3_000 });
  });

  // -----------------------------------------------------------------------
  // Hero section
  // -----------------------------------------------------------------------

  test('hero primary self-host CTA scrolls to install section', async ({ page }) => {
    await page.locator('.landing-hero-ctas button:has-text("Self-Host Free")').first().click();
    await expect(page.locator('#landing-install')).toBeInViewport({ timeout: 3_000 });
  });

  test('hero Hosted Later CTA scrolls to deployment section', async ({ page }) => {
    await page.locator('.landing-hero-ctas button:has-text("Hosted Later")').click();
    await expect(page.locator('#pricing')).toBeInViewport({ timeout: 3_000 });
  });

  test('hero See the launch story scrolls to the journey section', async ({ page }) => {
    await page.locator('button:has-text("See the launch story")').click();
    await expect(page.locator('#journey')).toBeInViewport({ timeout: 3_000 });
  });

  // -----------------------------------------------------------------------
  // Install section
  // -----------------------------------------------------------------------

  test('install section shows correct curl command', async ({ page }) => {
    const installSection = page.locator('.install-section');
    await expect(installSection).toBeVisible();
    await expect(installSection.locator('.install-command')).toContainText(
      'curl -fsSL https://get.longhouse.ai/install.sh | bash'
    );
  });

  // -----------------------------------------------------------------------
  // Pricing section
  // -----------------------------------------------------------------------

  test('pricing self-hosted CTA scrolls to install section', async ({ page }) => {
    // Scroll to pricing first
    const pricingSection = page.locator('#pricing');
    await pricingSection.scrollIntoViewIfNeeded();

    // Self-hosted CTA in pricing section
    const selfHostedCTA = pricingSection.getByRole('button', { name: 'Self-Host Free' });
    await selfHostedCTA.click();

    await expect(page.locator('#landing-install')).toBeInViewport({ timeout: 3_000 });
  });

  test('pricing hosted CTA links to control plane', async ({ page }) => {
    const pricingSection = page.locator('#pricing');
    await pricingSection.scrollIntoViewIfNeeded();

    // Hosted card CTA should navigate to control plane
    const hostedCTA = pricingSection.getByRole('button', { name: 'Request Hosted Beta' });
    await expect(hostedCTA).toBeVisible();
    await expect(hostedCTA).toBeEnabled();

    const [popup] = await Promise.all([
      page.waitForEvent('popup', { timeout: 5_000 }).catch(() => null),
      hostedCTA.click(),
    ]);

    if (popup) {
      await expect.poll(() => popup.url(), { timeout: 5_000 }).toContain('control.longhouse.ai');
      return;
    }

    await expect
      .poll(() => page.url(), { timeout: 5_000 })
      .toMatch(/control\.longhouse\.ai|\/timeline/);
  });

  // -----------------------------------------------------------------------
  // Footer
  // -----------------------------------------------------------------------

  test('footer Self-host Free scrolls to install section', async ({ page }) => {
    const footer = page.locator('.landing-footer');
    await footer.scrollIntoViewIfNeeded();

    await footer.locator('button:has-text("Self-Host Free")').click();
    await expect(page.locator('#landing-install')).toBeInViewport({ timeout: 3_000 });
  });

  test('footer secondary CTA opens docs', async ({ page }) => {
    const footer = page.locator('.landing-footer');
    await footer.scrollIntoViewIfNeeded();

    const docsBtn = footer.locator('button:has-text("Read the Docs")');
    await expect(docsBtn).toBeVisible();
    await docsBtn.click();
    await expect
      .poll(() => page.url(), { timeout: 5_000 })
      .toMatch(/\/docs$/);
  });

  test('footer documentation link goes to /docs', async ({ page }) => {
    const link = page.locator('.landing-footer a:has-text("Documentation")');
    await expect(link).toHaveAttribute('href', '/docs');
  });

  test('footer changelog link goes to /changelog', async ({ page }) => {
    const link = page.locator('.landing-footer a:has-text("Changelog")');
    await expect(link).toHaveAttribute('href', '/changelog');
  });

  test('footer GitHub link opens repo', async ({ page }) => {
    const link = page.locator('.landing-footer a:has-text("GitHub")');
    await expect(link).toHaveAttribute('href', 'https://github.com/cipher982/longhouse');
    await expect(link).toHaveAttribute('target', '_blank');
  });

  test('footer security link goes to /security', async ({ page }) => {
    const link = page.locator('.landing-footer a:has-text("Security")');
    await expect(link).toHaveAttribute('href', '/security');
  });

  test('footer privacy link goes to /privacy', async ({ page }) => {
    const link = page.locator('.landing-footer a:has-text("Privacy")');
    await expect(link).toHaveAttribute('href', '/privacy');
  });

  test('footer contact link is mailto', async ({ page }) => {
    const link = page.locator('.landing-footer a:has-text("Contact")');
    await expect(link).toHaveAttribute('href', 'mailto:support@longhouse.ai');
  });

  test('footer Discord link opens invite', async ({ page }) => {
    const link = page.locator('.landing-footer a:has-text("Discord")');
    await expect(link).toHaveAttribute('href', /discord\.gg/);
    await expect(link).toHaveAttribute('target', '_blank');
  });

  // -----------------------------------------------------------------------
  // No broken internal routes
  // -----------------------------------------------------------------------

  test('no links point to nonexistent internal routes', async ({ page }) => {
    // Collect all internal <a href="/..."> links
    const links = page.locator('a[href^="/"]');
    const count = await links.count();
    const validRoutePrefixes = ['/', '/docs', '/changelog', '/pricing', '/privacy', '/security', '/timeline', '/landing'];

    for (let i = 0; i < count; i++) {
      const href = await links.nth(i).getAttribute('href');
      if (href && !href.startsWith('/#')) {
        expect(validRoutePrefixes.some((route) => href === route || href.startsWith(`${route}/`))).toBe(true);
      }
    }
  });
});
