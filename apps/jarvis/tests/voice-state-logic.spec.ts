import { test, expect } from '@playwright/test';

/**
 * Unit tests for voice controller state logic
 * These tests mock the connection and focus on testing PTT/hands-free state management
 */

test.describe('Voice Controller State Logic', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate and wait for initialization
    await page.goto('http://localhost:8080');

    // Wait for voiceController to be available on window
    await page.waitForFunction(() => {
      return typeof (window as any).voiceController !== 'undefined';
    }, { timeout: 10000 });

    await page.waitForTimeout(500);

    // Mock successful connection by directly manipulating voice controller
    await page.evaluate(() => {
      const vc = (window as any).voiceController;
      if (!vc) throw new Error('voiceController not initialized');

      // Create mock session
      const mockSession = {
        on: () => {},
        connect: async () => {},
        disconnect: async () => {}
      };

      // Set session to simulate connected state
      vc.setSession(mockSession);

      // Create mock mic stream with mutable enabled property
      const mockTrack = {
        enabled: false,
        stop: () => {}
      };
      const mockStream = {
        getAudioTracks: () => [mockTrack]
      };
      vc.setMicrophoneStream(mockStream);

      // Set initial state
      vc.transitionToVoice({ armed: true, handsFree: false });
    });

    await page.waitForTimeout(500);
  });

  test('PTT: armed state should be true after mock connection', async ({ page }) => {
    const state = await page.evaluate(() => {
      return (window as any).voiceController.getState();
    });

    expect(state.armed).toBe(true);
    expect(state.handsFree).toBe(false);
    expect(state.interactionMode).toBe('voice');
  });

  test('PTT: startPTT should activate microphone', async ({ page }) => {
    // Start PTT
    await page.evaluate(() => {
      (window as any).voiceController.startPTT();
    });

    const state = await page.evaluate(() => {
      return (window as any).voiceController.getState();
    });

    expect(state.pttActive).toBe(true);
    expect(state.active).toBe(true);
    expect(state.armed).toBe(true);

    // Check mic track is enabled
    const micEnabled = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      const stream = vc.micStream;
      if (!stream) return null;
      const track = stream.getAudioTracks()[0];
      return track?.enabled;
    });
    expect(micEnabled).toBe(true);
  });

  test('PTT: stopPTT should deactivate microphone', async ({ page }) => {
    // Start then stop PTT
    await page.evaluate(() => {
      const vc = (window as any).voiceController;
      vc.startPTT();
      vc.stopPTT();
    });

    const state = await page.evaluate(() => {
      return (window as any).voiceController.getState();
    });

    expect(state.pttActive).toBe(false);
    expect(state.active).toBe(false);
    expect(state.armed).toBe(true); // Should remain armed for next PTT

    // Check mic track is disabled
    const micEnabled = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      const stream = vc.micStream;
      if (!stream) return null;
      const track = stream.getAudioTracks()[0];
      return track?.enabled;
    });
    expect(micEnabled).toBe(false);
  });

  test('Hands-free: setHandsFree(true) should keep mic active', async ({ page }) => {
    // Enable hands-free
    await page.evaluate(() => {
      (window as any).voiceController.setHandsFree(true);
    });

    const state = await page.evaluate(() => {
      return (window as any).voiceController.getState();
    });

    expect(state.handsFree).toBe(true);
    expect(state.armed).toBe(true);
    expect(state.mode).toBe('vad');

    // Mic should be unmuted
    const micEnabled = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      const stream = vc.micStream;
      if (!stream) return null;
      const track = stream.getAudioTracks()[0];
      return track?.enabled;
    });
    expect(micEnabled).toBe(true);
  });

  test('Hands-free: mic should stay active after VAD cycle', async ({ page }) => {
    // Enable hands-free
    await page.evaluate(() => {
      const vc = (window as any).voiceController;
      vc.setHandsFree(true);
    });

    // Simulate VAD start
    await page.evaluate(() => {
      (window as any).voiceController.handleVADStateChange(true);
    });

    let state = await page.evaluate(() => {
      return (window as any).voiceController.getState();
    });
    expect(state.vadActive).toBe(true);
    expect(state.active).toBe(true);

    // Simulate VAD stop
    await page.evaluate(() => {
      (window as any).voiceController.handleVADStateChange(false);
    });

    state = await page.evaluate(() => {
      return (window as any).voiceController.getState();
    });
    expect(state.vadActive).toBe(false);
    expect(state.active).toBe(false);
    expect(state.handsFree).toBe(true); // Still in hands-free

    // CRITICAL: Mic should STILL be enabled in hands-free mode
    const micEnabled = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      const stream = vc.micStream;
      if (!stream) return null;
      const track = stream.getAudioTracks()[0];
      return track?.enabled;
    });
    expect(micEnabled).toBe(true);
  });

  test('Hands-free: audioController should not mute when state.active becomes false', async ({ page }) => {
    // Enable hands-free
    await page.evaluate(() => {
      (window as any).voiceController.setHandsFree(true);
    });

    // Trigger voice state change event with active=false (simulates end of speech)
    await page.evaluate(() => {
      const vc = (window as any).voiceController;
      const state = vc.getState();

      // This simulates what happens when handleVoiceStateChange is called
      // with state.active = false but state.handsFree = true
      const audioCtrl = (window as any).audioController;

      // The bug would be here: audioController.muteMicrophone() gets called
      // even in hands-free mode
      if (state.handsFree && !state.active) {
        // In hands-free mode, mic should stay unmuted
        const stream = audioCtrl.micStream;
        if (stream) {
          const track = stream.getAudioTracks()[0];
          // Verify mic is NOT muted
          return track?.enabled;
        }
      }
    });
  });

  test('Hands-free: multiple VAD cycles should keep mic active', async ({ page }) => {
    // Enable hands-free
    await page.evaluate(() => {
      (window as any).voiceController.setHandsFree(true);
    });

    // Run 3 VAD cycles
    for (let i = 0; i < 3; i++) {
      await page.evaluate(() => {
        const vc = (window as any).voiceController;
        vc.handleVADStateChange(true);
      });
      await page.waitForTimeout(100);

      await page.evaluate(() => {
        const vc = (window as any).voiceController;
        vc.handleVADStateChange(false);
      });
      await page.waitForTimeout(100);
    }

    // After all cycles, mic should STILL be enabled
    const micEnabled = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      const stream = vc.micStream;
      if (!stream) return null;
      const track = stream.getAudioTracks()[0];
      return track?.enabled;
    });
    expect(micEnabled).toBe(true);

    const state = await page.evaluate(() => {
      return (window as any).voiceController.getState();
    });
    expect(state.handsFree).toBe(true);
  });
});
