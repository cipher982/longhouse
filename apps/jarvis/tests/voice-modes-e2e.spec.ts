import { test, expect } from '@playwright/test';

/**
 * Comprehensive e2e tests for PTT and Hands-free modes
 * Uses test mode to mock OpenAI Realtime connection
 *
 * SKIP in CI/Docker - requires voiceController to be fully initialized
 */

test.describe.skip('Voice Modes E2E', () => {
  test.beforeEach(async ({ page, context }) => {
    await context.grantPermissions(['microphone']);
    await page.goto('/');

    // Wait for app initialization
    await page.waitForFunction(() => {
      return typeof (window as any).__jarvisTestHelpers__ !== 'undefined';
    }, { timeout: 10000 });
  });

  test('PTT Mode: should connect with mock and enable PTT', async ({ page }) => {
    // Use test helper to mock connection
    await page.evaluate(async () => {
      const helpers = (window as any).__jarvisTestHelpers__;
      helpers.enableTestMode();
      await helpers.mockConnect();
    });

    // Wait a bit for state to settle
    await page.waitForTimeout(500);

    // Verify connection state
    const state = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      return {
        isConnected: vc.isConnected(),
        interactionMode: vc.getState().interactionMode,
        handsFree: vc.getState().handsFree
      };
    });

    expect(state.isConnected).toBe(true);
    expect(state.interactionMode).toBe('voice');
    expect(state.handsFree).toBe(false);
  });

  test('PTT Mode: pressing button should activate microphone', async ({ page }) => {
    // Mock connection
    await page.evaluate(async () => {
      await (window as any).__jarvisTestHelpers__.mockConnect();
    });
    await page.waitForTimeout(500);

    const pttBtn = page.locator('#pttBtn');

    // Press button (mousedown)
    await pttBtn.dispatchEvent('mousedown');
    await page.waitForTimeout(100);

    // Check PTT is active and mic is enabled
    const duringPress = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      const ac = (window as any).audioController;
      const track = ac.micStream?.getAudioTracks()[0];

      return {
        pttActive: vc.getState().pttActive,
        active: vc.getState().active,
        micEnabled: track?.enabled
      };
    });

    expect(duringPress.pttActive).toBe(true);
    expect(duringPress.active).toBe(true);
    expect(duringPress.micEnabled).toBe(true);

    // Release button (mouseup)
    await pttBtn.dispatchEvent('mouseup');
    await page.waitForTimeout(100);

    // Check PTT is inactive but ready for next press
    const afterRelease = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      const ac = (window as any).audioController;
      const track = ac.micStream?.getAudioTracks()[0];

      return {
        pttActive: vc.getState().pttActive,
        active: vc.getState().active,
        interactionMode: vc.getState().interactionMode,
        micEnabled: track?.enabled
      };
    });

    expect(afterRelease.pttActive).toBe(false);
    expect(afterRelease.active).toBe(false);
    expect(afterRelease.interactionMode).toBe('voice'); // Should stay in voice mode
    expect(afterRelease.micEnabled).toBe(false);
  });

  test('PTT Mode: multiple press cycles should work', async ({ page }) => {
    await page.evaluate(async () => {
      await (window as any).__jarvisTestHelpers__.mockConnect();
    });
    await page.waitForTimeout(500);

    const pttBtn = page.locator('#pttBtn');

    // Do 3 PTT cycles
    for (let i = 0; i < 3; i++) {
      // Press
      await pttBtn.dispatchEvent('mousedown');
      await page.waitForTimeout(100);

      const pressing = await page.evaluate(() => {
        const vc = (window as any).voiceController;
        return {
          pttActive: vc.getState().pttActive,
          active: vc.getState().active
        };
      });

      expect(pressing.pttActive).toBe(true);
      expect(pressing.active).toBe(true);

      // Release
      await pttBtn.dispatchEvent('mouseup');
      await page.waitForTimeout(100);

      const released = await page.evaluate(() => {
        const vc = (window as any).voiceController;
        return {
          pttActive: vc.getState().pttActive,
          interactionMode: vc.getState().interactionMode
        };
      });

      expect(released.pttActive).toBe(false);
      expect(released.interactionMode).toBe('voice'); // Should stay in voice mode
    }
  });

  test('Hands-free Mode: toggle should enable continuous mic', async ({ page }) => {
    await page.evaluate(async () => {
      await (window as any).__jarvisTestHelpers__.mockConnect();
    });
    await page.waitForTimeout(500);

    const handsFreeToggle = page.locator('#handsFreeToggle');

    // Enable hands-free
    await handsFreeToggle.click();
    await page.waitForTimeout(300);

    // Check state
    const afterToggle = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      const ac = (window as any).audioController;
      const track = ac.micStream?.getAudioTracks()[0];

      return {
        handsFree: vc.getState().handsFree,
        interactionMode: vc.getState().interactionMode,
        mode: vc.getState().mode,
        micEnabled: track?.enabled,
        toggleState: document.getElementById('handsFreeToggle')?.getAttribute('aria-checked')
      };
    });

    expect(afterToggle.handsFree).toBe(true);
    expect(afterToggle.interactionMode).toBe('voice');
    expect(afterToggle.mode).toBe('vad');
    expect(afterToggle.micEnabled).toBe(true); // Mic should be on
    expect(afterToggle.toggleState).toBe('true');
  });

  test('Hands-free Mode: mic stays active after VAD cycle', async ({ page }) => {
    await page.evaluate(async () => {
      await (window as any).__jarvisTestHelpers__.mockConnect();
    });
    await page.waitForTimeout(500);

    // Enable hands-free
    await page.locator('#handsFreeToggle').click();
    await page.waitForTimeout(300);

    // Simulate VAD cycle (speech detected, then stopped)
    await page.evaluate(() => {
      const vc = (window as any).voiceController;
      vc.handleVADStateChange(true); // Speech detected
    });
    await page.waitForTimeout(100);

    const duringSpeech = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      return {
        vadActive: vc.getState().vadActive,
        active: vc.getState().active
      };
    });

    expect(duringSpeech.vadActive).toBe(true);
    expect(duringSpeech.active).toBe(true);

    // Speech stopped
    await page.evaluate(() => {
      const vc = (window as any).voiceController;
      vc.handleVADStateChange(false);
    });
    await page.waitForTimeout(100);

    const afterSpeech = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      const ac = (window as any).audioController;
      const track = ac.micStream?.getAudioTracks()[0];

      return {
        vadActive: vc.getState().vadActive,
        active: vc.getState().active,
        handsFree: vc.getState().handsFree,
        micEnabled: track?.enabled // CRITICAL: should still be true
      };
    });

    expect(afterSpeech.vadActive).toBe(false);
    expect(afterSpeech.active).toBe(false);
    expect(afterSpeech.handsFree).toBe(true);
    expect(afterSpeech.micEnabled).toBe(true); // Mic should STAY on in hands-free
  });

  test('Hands-free Mode: multiple VAD cycles preserve mic state', async ({ page }) => {
    await page.evaluate(async () => {
      await (window as any).__jarvisTestHelpers__.mockConnect();
    });
    await page.waitForTimeout(500);

    await page.locator('#handsFreeToggle').click();
    await page.waitForTimeout(300);

    // Run 3 VAD cycles
    for (let i = 0; i < 3; i++) {
      // Speech start
      await page.evaluate(() => {
        (window as any).voiceController.handleVADStateChange(true);
      });
      await page.waitForTimeout(100);

      // Speech stop
      await page.evaluate(() => {
        (window as any).voiceController.handleVADStateChange(false);
      });
      await page.waitForTimeout(100);
    }

    // After all cycles, mic should still be enabled
    const final = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      const ac = (window as any).audioController;
      const track = ac.micStream?.getAudioTracks()[0];

      return {
        handsFree: vc.getState().handsFree,
        micEnabled: track?.enabled
      };
    });

    expect(final.handsFree).toBe(true);
    expect(final.micEnabled).toBe(true);
  });

  test('Mode Switch: toggling hands-free off returns to PTT', async ({ page }) => {
    await page.evaluate(async () => {
      await (window as any).__jarvisTestHelpers__.mockConnect();
    });
    await page.waitForTimeout(500);

    const handsFreeToggle = page.locator('#handsFreeToggle');

    // Enable hands-free
    await handsFreeToggle.click();
    await page.waitForTimeout(300);

    let state = await page.evaluate(() => {
      return (window as any).voiceController.getState();
    });
    expect(state.handsFree).toBe(true);
    expect(state.mode).toBe('vad');

    // Disable hands-free
    await handsFreeToggle.click();
    await page.waitForTimeout(300);

    state = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      const ac = (window as any).audioController;
      const track = ac.micStream?.getAudioTracks()[0];

      return {
        ...vc.getState(),
        micEnabled: track?.enabled
      };
    });

    expect(state.handsFree).toBe(false);
    expect(state.mode).toBe('ptt');
    expect(state.interactionMode).toBe('voice'); // Should be in voice mode for PTT
    expect(state.micEnabled).toBe(false); // Mic should be off in PTT mode
  });

  test('UI State: button should show correct visual states', async ({ page }) => {
    await page.evaluate(async () => {
      await (window as any).__jarvisTestHelpers__.mockConnect();
    });
    await page.waitForTimeout(500);

    const pttBtn = page.locator('#pttBtn');
    const statusText = page.locator('.voice-status-text');

    // Initial state after connection
    let hasReadyClass = await pttBtn.evaluate(el => el.classList.contains('ready'));
    expect(hasReadyClass).toBe(true);

    let text = await statusText.textContent();
    expect(text).toContain('Ready');

    // During PTT
    await pttBtn.dispatchEvent('mousedown');
    await page.waitForTimeout(100);

    let hasSpeakingClass = await pttBtn.evaluate(el => el.classList.contains('speaking'));
    expect(hasSpeakingClass).toBe(true);

    // After PTT
    await pttBtn.dispatchEvent('mouseup');
    await page.waitForTimeout(100);

    hasReadyClass = await pttBtn.evaluate(el => el.classList.contains('ready'));
    expect(hasReadyClass).toBe(true);
  });
});
