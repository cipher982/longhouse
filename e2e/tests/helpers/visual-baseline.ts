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

export function getPlatformScopedSnapshotName(name: string, platform: string = process.platform): string {
  return platform === 'linux' ? `${name}-linux` : name;
}

export function getPlatformScopedDesktopSnapshotFile(
  name: string,
  platform: string = process.platform,
): string {
  return `${getPlatformScopedSnapshotName(name, platform)}-chromium-darwin.png`;
}
