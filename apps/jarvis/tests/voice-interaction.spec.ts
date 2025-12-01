import { test, expect } from '@playwright/test';

test.describe('Voice Interaction Flow', () => {
  test.beforeEach(async ({ page, context }) => {
    // Grant microphone permissions
    await context.grantPermissions(['microphone']);

    // Navigate to Jarvis
    await page.goto('http://localhost:8080');
    await page.waitForSelector('#transcript');

    // Wait for app initialization
    await page.waitForTimeout(2000);
  });

  test('should initialize with correct UI state', async ({ page }) => {
    // Check initial button state
    const pttBtn = page.locator('#pttBtn');
    await expect(pttBtn).toBeVisible();

    // Should be in idle state initially
    const hasIdleClass = await pttBtn.evaluate((el) => el.classList.contains('idle'));
    expect(hasIdleClass).toBe(true);

    // Status text should indicate ready to start
    const statusText = page.locator('.voice-status-text');
    await expect(statusText).toBeVisible();
    const text = await statusText.textContent();
    expect(text).toMatch(/tap to talk|start voice|ready/i);
  });

  test('should connect when clicking voice button', async ({ page }) => {
    const pttBtn = page.locator('#pttBtn');
    const statusText = page.locator('.voice-status-text');

    // Click to connect
    await pttBtn.click();

    // Should show connecting state
    await page.waitForTimeout(500);
    let text = await statusText.textContent();
    expect(text?.toLowerCase()).toContain('connect');

    // Wait for connection to complete (up to 10 seconds)
    await page.waitForFunction(() => {
      const btn = document.getElementById('pttBtn');
      return btn?.classList.contains('ready') || btn?.classList.contains('speaking');
    }, { timeout: 10000 });

    // Should transition to ready state
    const hasReadyClass = await pttBtn.evaluate((el) => el.classList.contains('ready'));
    expect(hasReadyClass).toBe(true);

    // Status should show ready to talk
    text = await statusText.textContent();
    expect(text).toMatch(/ready|hold to talk/i);
  });

  test('PTT mode: should activate microphone on button press', async ({ page }) => {
    const pttBtn = page.locator('#pttBtn');

    // Connect first
    await pttBtn.click();
    await page.waitForFunction(() => {
      return document.getElementById('pttBtn')?.classList.contains('ready');
    }, { timeout: 10000 });

    // Check voiceController is in voice mode
    const isVoiceMode = await page.evaluate(() => {
      return (window as any).voiceController?.getState().interactionMode === 'voice';
    });
    expect(isVoiceMode).toBe(true);

    // Press and hold button
    await pttBtn.dispatchEvent('mousedown');
    await page.waitForTimeout(100);

    // Check if PTT is active
    const isPTTActive = await page.evaluate(() => {
      return (window as any).voiceController?.getState().pttActive;
    });
    expect(isPTTActive).toBe(true);

    // Check if mic is unmuted
    const isMicActive = await page.evaluate(() => {
      const audioCtrl = (window as any).audioController;
      if (!audioCtrl || !audioCtrl.micStream) return false;
      const track = audioCtrl.micStream.getAudioTracks()[0];
      return track?.enabled === true;
    });
    expect(isMicActive).toBe(true);

    // Release button
    await pttBtn.dispatchEvent('mouseup');
    await page.waitForTimeout(100);

    // Check if PTT is inactive
    const isPTTInactive = await page.evaluate(() => {
      return (window as any).voiceController?.getState().pttActive === false;
    });
    expect(isPTTInactive).toBe(true);

    // Check if mic is muted again
    const isMicMuted = await page.evaluate(() => {
      const audioCtrl = (window as any).audioController;
      if (!audioCtrl || !audioCtrl.micStream) return true;
      const track = audioCtrl.micStream.getAudioTracks()[0];
      return track?.enabled === false;
    });
    expect(isMicMuted).toBe(true);
  });

  test('Hands-free mode: should keep microphone active after speech', async ({ page }) => {
    const pttBtn = page.locator('#pttBtn');
    const handsFreeToggle = page.locator('#handsFreeToggle');

    // Connect first
    await pttBtn.click();
    await page.waitForFunction(() => {
      return document.getElementById('pttBtn')?.classList.contains('ready');
    }, { timeout: 10000 });

    // Enable hands-free mode
    await handsFreeToggle.click();
    await page.waitForTimeout(500);

    // Check hands-free is enabled
    const isHandsFree = await page.evaluate(() => {
      return (window as any).voiceController?.getState().handsFree;
    });
    expect(isHandsFree).toBe(true);

    // Check toggle UI state
    const toggleState = await handsFreeToggle.getAttribute('aria-checked');
    expect(toggleState).toBe('true');

    // Mic should be unmuted in hands-free mode
    const isMicActive = await page.evaluate(() => {
      const audioCtrl = (window as any).audioController;
      if (!audioCtrl || !audioCtrl.micStream) return false;
      const track = audioCtrl.micStream.getAudioTracks()[0];
      return track?.enabled === true;
    });
    expect(isMicActive).toBe(true);

    // Simulate speech detection
    await page.evaluate(() => {
      (window as any).voiceController?.handleVADStateChange(true);
    });
    await page.waitForTimeout(100);

    // Simulate speech stop
    await page.evaluate(() => {
      (window as any).voiceController?.handleVADStateChange(false);
    });
    await page.waitForTimeout(100);

    // Mic should STILL be active in hands-free mode
    const isMicStillActive = await page.evaluate(() => {
      const audioCtrl = (window as any).audioController;
      if (!audioCtrl || !audioCtrl.micStream) return false;
      const track = audioCtrl.micStream.getAudioTracks()[0];
      return track?.enabled === true;
    });
    expect(isMicStillActive).toBe(true);
  });

  test('should visualize audio when speaking', async ({ page }) => {
    const pttBtn = page.locator('#pttBtn');

    // Connect first
    await pttBtn.click();
    await page.waitForFunction(() => {
      return document.getElementById('pttBtn')?.classList.contains('ready');
    }, { timeout: 10000 });

    // Press button to activate PTT
    await pttBtn.dispatchEvent('mousedown');
    await page.waitForTimeout(100);

    // Check if listening mode is active
    const isListening = await page.evaluate(() => {
      return document.body.classList.contains('listening-mode');
    });
    expect(isListening).toBe(true);

    // Button should show speaking state
    const hasSpeakingClass = await pttBtn.evaluate((el) => el.classList.contains('speaking'));
    expect(hasSpeakingClass).toBe(true);

    // Release button
    await pttBtn.dispatchEvent('mouseup');
    await page.waitForTimeout(100);

    // Listening mode should be off
    const isListeningAfter = await page.evaluate(() => {
      return document.body.classList.contains('listening-mode');
    });
    expect(isListeningAfter).toBe(false);
  });

  test('should sync hands-free toggle with voice controller state', async ({ page }) => {
    const pttBtn = page.locator('#pttBtn');
    const handsFreeToggle = page.locator('#handsFreeToggle');

    // Connect first
    await pttBtn.click();
    await page.waitForFunction(() => {
      return document.getElementById('pttBtn')?.classList.contains('ready');
    }, { timeout: 10000 });

    // Initially should be off
    let toggleState = await handsFreeToggle.getAttribute('aria-checked');
    expect(toggleState).toBe('false');

    // Enable hands-free
    await handsFreeToggle.click();
    await page.waitForTimeout(200);

    toggleState = await handsFreeToggle.getAttribute('aria-checked');
    expect(toggleState).toBe('true');

    // Voice controller should reflect this
    const vcState = await page.evaluate(() => {
      return (window as any).voiceController?.getState().handsFree;
    });
    expect(vcState).toBe(true);

    // Disable hands-free
    await handsFreeToggle.click();
    await page.waitForTimeout(200);

    toggleState = await handsFreeToggle.getAttribute('aria-checked');
    expect(toggleState).toBe('false');

    const vcStateAfter = await page.evaluate(() => {
      return (window as any).voiceController?.getState().handsFree;
    });
    expect(vcStateAfter).toBe(false);
  });

  test('should handle rapid PTT presses without issues', async ({ page }) => {
    const pttBtn = page.locator('#pttBtn');

    // Connect first
    await pttBtn.click();
    await page.waitForFunction(() => {
      return document.getElementById('pttBtn')?.classList.contains('ready');
    }, { timeout: 10000 });

    // Rapid press/release cycles
    for (let i = 0; i < 5; i++) {
      await pttBtn.dispatchEvent('mousedown');
      await page.waitForTimeout(50);
      await pttBtn.dispatchEvent('mouseup');
      await page.waitForTimeout(50);
    }

    // Should still be in ready state
    const hasReadyClass = await pttBtn.evaluate((el) => el.classList.contains('ready'));
    expect(hasReadyClass).toBe(true);

    // Voice controller should not be in stuck state
    const isPTTActive = await page.evaluate(() => {
      return (window as any).voiceController?.getState().pttActive;
    });
    expect(isPTTActive).toBe(false);
  });
});
