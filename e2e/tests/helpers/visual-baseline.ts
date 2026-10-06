import type { Page } from '@playwright/test';

const VISUAL_FONT_FAMILY = '__longhouse_visual_inter';

const VISUAL_FONT_CSS = `
  @font-face {
    font-family: '${VISUAL_FONT_FAMILY}';
    src: url('/test-fonts/inter-400.ttf') format('truetype');
    font-weight: 400;
    font-style: normal;
    font-display: block;
  }

  @font-face {
    font-family: '${VISUAL_FONT_FAMILY}';
    src: url('/test-fonts/inter-500.ttf') format('truetype');
    font-weight: 500;
    font-style: normal;
    font-display: block;
  }

  @font-face {
    font-family: '${VISUAL_FONT_FAMILY}';
    src: url('/test-fonts/inter-600.ttf') format('truetype');
    font-weight: 600;
    font-style: normal;
    font-display: block;
  }

  @font-face {
    font-family: '${VISUAL_FONT_FAMILY}';
    src: url('/test-fonts/inter-700.ttf') format('truetype');
    font-weight: 700;
    font-style: normal;
    font-display: block;
  }

  :root {
    --font-family-display: '${VISUAL_FONT_FAMILY}', sans-serif !important;
    --font-family-base: '${VISUAL_FONT_FAMILY}', sans-serif !important;
  }
`;

export async function installDeterministicVisualFonts(page: Page): Promise<void> {
  await page.addStyleTag({ content: VISUAL_FONT_CSS });
  await page.evaluate(async (fontFamily) => {
    if (!document.fonts) {
      return;
    }

    await Promise.all([
      document.fonts.load(`400 16px ${fontFamily}`),
      document.fonts.load(`500 16px ${fontFamily}`),
      document.fonts.load(`600 16px ${fontFamily}`),
      document.fonts.load(`700 16px ${fontFamily}`),
    ]);
    await document.fonts.ready;
  }, VISUAL_FONT_FAMILY);
}

export function getPlatformScopedSnapshotName(name: string): string {
  return name;
}

/**
 * Baselines are Linux renders (playwright.config.js pins the suffix), made on
 * the crunch VM so they match the environment CI runs in. A macOS run renders
 * fonts differently and should not be compared against them.
 */
export function getPlatformScopedDesktopSnapshotFile(
  name: string,
): string {
  return `${getPlatformScopedSnapshotName(name)}-chromium-linux.png`;
}

/**
 * The Devices page lists every device token on the backend, and each test's
 * request fixture mints one that a database reset does not clear, so the real
 * list grows with however many tests ran first. Baselines read this fixed list.
 * Dates sit more than a week back, where the page prints absolute dates.
 */
const BASELINE_DEVICE_TOKENS = {
  tokens: [
    {
      id: '00000000-0000-4000-8000-000000000001',
      device_id: 'studio-mac',
      created_at: '2026-01-05T15:00:00Z',
      last_used_at: '2026-01-09T15:00:00Z',
      revoked_at: null,
      is_valid: true,
    },
    {
      id: '00000000-0000-4000-8000-000000000002',
      device_id: 'build-server',
      created_at: '2026-01-02T15:00:00Z',
      last_used_at: null,
      revoked_at: null,
      is_valid: true,
    },
  ],
  total: 2,
};

export async function stubBaselineDeviceTokens(page: Page): Promise<void> {
  await page.route('**/api/devices/tokens*', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await route.fulfill({ json: BASELINE_DEVICE_TOKENS });
  });
}

/**
 * Content that legitimately differs between runs: the connect command embeds
 * this host's origin, and E2E backends listen on a random port.
 */
export function volatileRegions(page: Page) {
  return [
    page.locator('[data-testid="connect-machine-command"]'),
    page.locator('.cli-instructions code'),
  ];
}
