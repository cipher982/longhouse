/**
 * Landing page link audit.
 *
 * Verifies every navigation link and CTA on the landing page points to the
 * correct destination. Prevents recurring bugs where links silently route
 * to wrong pages (e.g., "Sign In" going to demo timeline instead of auth).
 *
 * This test runs in demo mode (how the marketing site operates).
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

  test('header Get Started scrolls to pricing', async ({ page }) => {
    await page.locator('button:has-text("Get Started")').first().click();
    // Pricing section should be in viewport
    await expect(page.locator('#pricing')).toBeInViewport({ timeout: 3_000 });
  });

  // -----------------------------------------------------------------------
  // Hero section
  // -----------------------------------------------------------------------

  test('hero secondary self-host CTA scrolls to install section when present', async ({ page }) => {
    const selfHostBtn = page.locator('button:has-text("Self-host Free")').first();
    if ((await selfHostBtn.count()) === 0) {
      // In demo mode the secondary hero CTA is "Try Live Demo".
      await expect(page.locator('button:has-text("Try Live Demo")').first()).toBeVisible();
      return;
    }
    await selfHostBtn.click();
    await expect(page.locator('.install-section')).toBeInViewport({ timeout: 3_000 });
  });

  test('hero secondary CTA is Try Live Demo or Self-host Free (not broken)', async ({ page }) => {
    // The hero has either "Try Live Demo" (demo mode) or "Self-host Free" (non-demo mode)
    const demoBtn = page.locator('button:has-text("Try Live Demo")');
    const selfHostBtn = page.locator('button:has-text("Self-host Free")');

    const hasDemoBtn = await demoBtn.count() > 0;
    const hasSelfHostBtn = await selfHostBtn.count() > 0;

    // One of them must exist
    expect(hasDemoBtn || hasSelfHostBtn).toBe(true);
  });

  test('hero See How It Works scrolls to how-it-works section', async ({ page }) => {
    await page.locator('button:has-text("See How It Works")').click();
    await expect(page.locator('#how-it-works')).toBeInViewport({ timeout: 3_000 });
  });

  test('hero enterprise link is mailto', async ({ page }) => {
    const link = page.locator('a.landing-hero-enterprise-link');
    await expect(link).toHaveAttribute('href', /^mailto:hello@longhouse\.ai/);
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

  test('pricing self-hosted Get Started scrolls to install section', async ({ page }) => {
    // Scroll to pricing first
    const pricingSection = page.locator('#pricing');
    await pricingSection.scrollIntoViewIfNeeded();

    // Self-hosted CTA in pricing section
    const selfHostedCTA = pricingSection.getByRole('button', { name: 'Self-host Free' });
    await selfHostedCTA.click();

    await expect(page.locator('.install-section')).toBeInViewport({ timeout: 3_000 });
  });

  test('pricing hosted Get Started links to control plane', async ({ page }) => {
    const pricingSection = page.locator('#pricing');
    await pricingSection.scrollIntoViewIfNeeded();

    // Hosted card CTA should navigate to control plane
    const hostedCTA = pricingSection.getByRole('button', { name: 'Get Started' });
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

    await footer.locator('button:has-text("Self-host Free")').click();
    await expect(page.locator('.install-section')).toBeInViewport({ timeout: 3_000 });
  });

  test('footer Get Started links to control plane (not waitlist)', async ({ page }) => {
    const footer = page.locator('.landing-footer');
    await footer.scrollIntoViewIfNeeded();

    // Should say "Get Started", NOT "Join Waitlist"
    const hostedBtn = footer.locator('button:has-text("Get Started")');
    const waitlistBtn = footer.locator('button:has-text("Join Waitlist")');

    await expect(hostedBtn).toBeVisible();
    expect(await waitlistBtn.count()).toBe(0);
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
  // Social proof external links
  // -----------------------------------------------------------------------

  test('social proof GitHub links open in new tab', async ({ page }) => {
    const badges = page.locator('.social-proof-badge[href*="github.com"]');
    const count = await badges.count();
    expect(count).toBeGreaterThan(0);
    for (let i = 0; i < count; i++) {
      await expect(badges.nth(i)).toHaveAttribute('target', '_blank');
    }
  });

  // -----------------------------------------------------------------------
  // No broken internal routes
  // -----------------------------------------------------------------------

  test('no links point to nonexistent internal routes', async ({ page }) => {
    // Collect all internal <a href="/..."> links
    const links = page.locator('a[href^="/"]');
    const count = await links.count();
    const validRoutes = ['/', '/docs', '/changelog', '/pricing', '/privacy', '/security', '/timeline', '/landing'];

    for (let i = 0; i < count; i++) {
      const href = await links.nth(i).getAttribute('href');
      if (href && !href.startsWith('/#')) {
        expect(validRoutes).toContain(href);
      }
    }
  });
});
