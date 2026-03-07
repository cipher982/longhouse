import { expect, test } from '@playwright/test';

test.describe('Runner onboarding install modes', () => {
  test('runners page exposes desktop and server install commands', async ({ page }) => {
    await page.goto('/runners');
    await page.waitForSelector('[data-ready="true"]', { timeout: 15_000 });

    const addRunnerButton = page.getByTestId('runners-add-button');
    await expect(addRunnerButton).toBeVisible();
    await addRunnerButton.click();

    const modal = page.getByTestId('add-runner-modal');
    await expect(modal).toBeVisible();

    const command = page.getByTestId('add-runner-command');
    await expect(command).toContainText('curl -fsSL', { timeout: 15_000 });
    await expect(command).toContainText('/api/runners/install.sh');
    await expect(command).toContainText('ENROLL_TOKEN=');
    await expect(command).not.toContainText('RUNNER_INSTALL_MODE=server');

    const desktopMode = page.getByTestId('runner-install-mode-desktop');
    const serverMode = page.getByTestId('runner-install-mode-server');
    await expect(desktopMode).toBeVisible();
    await expect(serverMode).toBeVisible();

    await serverMode.click();
    await expect(command).toContainText('RUNNER_INSTALL_MODE=server');
    await expect(page.getByText('Always-on Linux server/VM: installs a system service that survives logout and reboot.')).toBeVisible();
    await expect(page.getByText('Installs as a Linux system service')).toBeVisible();

    await desktopMode.click();
    await expect(command).not.toContainText('RUNNER_INSTALL_MODE=server');
    await expect(page.getByText('Personal machine: installs as launchd on macOS or a systemd user service on Linux.')).toBeVisible();
  });
});
