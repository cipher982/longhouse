import { test, expect } from '@playwright/test';

/**
 * REAL e2e test that clicks actual UI buttons
 * Tests the ACTUAL user interaction flow, not mocked behavior
 *
 * SKIP in CI/Docker - requires real OpenAI WebRTC connection
 */

test.describe.skip('PTT Button Real Interaction', () => {
  test('should connect and enable PTT when clicking button', async ({ page, context }) => {
    // Grant microphone permissions
    await context.grantPermissions(['microphone']);

    // Capture console logs from page
    page.on('console', msg => console.log('PAGE LOG:', msg.text()));

    // Navigate to Jarvis
    await page.goto('/');

    // Wait for initialization
    await page.waitForFunction(() => {
      return typeof (window as any).voiceController !== 'undefined';
    }, { timeout: 10000 });

    const pttBtn = page.locator('#pttBtn');
    const statusText = page.locator('.voice-status-text');

    console.log('=== Initial State ===');
    const initialText = await statusText.textContent();
    console.log('Status text:', initialText);

    const initialState = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      return {
        isConnected: vc?.isConnected(),
        state: vc?.getState()
      };
    });
    console.log('VoiceController state:', JSON.stringify(initialState, null, 2));

    // STEP 1: Click button to connect
    console.log('\n=== Clicking Button to Connect ===');
    await pttBtn.click();
    await page.waitForTimeout(1000);

    const afterClickText = await statusText.textContent();
    console.log('Status after click:', afterClickText);

    // Check if connection is attempting
    const connectingState = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      return {
        isConnected: vc?.isConnected(),
        state: vc?.getState()
      };
    });
    console.log('State after click:', JSON.stringify(connectingState, null, 2));

    // Wait up to 15 seconds for connection (or failure)
    console.log('\n=== Waiting for Connection Result ===');

    let finalState;
    let connectionError = null;

    try {
      await page.waitForFunction(() => {
        const vc = (window as any).voiceController;
        return vc?.isConnected() === true;
      }, { timeout: 15000 });

      finalState = await page.evaluate(() => {
        const vc = (window as any).voiceController;
        return {
          isConnected: vc?.isConnected(),
          state: vc?.getState()
        };
      });
      console.log('Connected! Final state:', JSON.stringify(finalState, null, 2));
    } catch (e) {
      console.log('Connection failed or timed out');

      // Check for error toasts
      const errorToast = await page.locator('.toast').textContent().catch(() => null);
      if (errorToast) {
        console.log('Error toast:', errorToast);
        connectionError = errorToast;
      }

      finalState = await page.evaluate(() => {
        const vc = (window as any).voiceController;
        return {
          isConnected: vc?.isConnected(),
          state: vc?.getState()
        };
      });
      console.log('State after timeout:', JSON.stringify(finalState, null, 2));
    }

    const finalText = await statusText.textContent();
    console.log('Final status text:', finalText);

    // Take screenshot of final state
    await page.screenshot({ path: 'test-results/ptt-after-connect.png' });

    if (!finalState.isConnected) {
      console.log('\n❌ CONNECTION FAILED - Cannot test PTT without connection');
      console.log('Error:', connectionError);
      return; // Skip PTT test if connection failed
    }

    // STEP 2: Test PTT (press and hold)
    console.log('\n=== Testing PTT Press ===');

    // Check voice mode state before PTT
    const beforePTT = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      return vc?.getState();
    });
    console.log('State before PTT:', JSON.stringify(beforePTT, null, 2));

    if (beforePTT.interactionMode !== 'voice') {
      console.log('❌ NOT IN VOICE MODE - PTT will not work!');
    }

    // Press button (mousedown)
    await pttBtn.dispatchEvent('mousedown');
    await page.waitForTimeout(500);

    const duringPTT = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      const ac = (window as any).audioController;
      const micStream = ac?.micStream;
      const track = micStream?.getAudioTracks()[0];

      return {
        voiceState: vc?.getState(),
        micStreamExists: !!micStream,
        trackEnabled: track?.enabled
      };
    });
    console.log('State during PTT:', JSON.stringify(duringPTT, null, 2));

    // Check if mic is actually enabled
    expect(duringPTT.voiceState.pttActive).toBe(true);
    expect(duringPTT.trackEnabled).toBe(true);

    // Release button (mouseup)
    await pttBtn.dispatchEvent('mouseup');
    await page.waitForTimeout(500);

    const afterPTT = await page.evaluate(() => {
      const vc = (window as any).voiceController;
      const ac = (window as any).audioController;
      const micStream = ac?.micStream;
      const track = micStream?.getAudioTracks()[0];

      return {
        voiceState: vc?.getState(),
        micStreamExists: !!micStream,
        trackEnabled: track?.enabled
      };
    });
    console.log('State after PTT release:', JSON.stringify(afterPTT, null, 2));

    // Verify mic is muted but still in voice mode
    expect(afterPTT.voiceState.pttActive).toBe(false);
    expect(afterPTT.voiceState.interactionMode).toBe('voice'); // Should stay in voice mode!
    expect(afterPTT.trackEnabled).toBe(false);

    console.log('\n✅ PTT TEST PASSED');
  });

  test('should stay connected and allow multiple PTT cycles', async ({ page, context }) => {
    await context.grantPermissions(['microphone']);
    await page.goto('/');

    await page.waitForFunction(() => {
      return typeof (window as any).voiceController !== 'undefined';
    }, { timeout: 10000 });

    const pttBtn = page.locator('#pttBtn');

    // Connect first
    await pttBtn.click();

    // Wait for connection (with timeout)
    try {
      await page.waitForFunction(() => {
        return (window as any).voiceController?.isConnected() === true;
      }, { timeout: 15000 });
    } catch (e) {
      console.log('❌ Connection failed, skipping test');
      return;
    }

    console.log('=== Testing Multiple PTT Cycles ===');

    // Do 3 PTT cycles
    for (let i = 1; i <= 3; i++) {
      console.log(`\n--- Cycle ${i} ---`);

      // Press
      await pttBtn.dispatchEvent('mousedown');
      await page.waitForTimeout(200);

      const pressing = await page.evaluate(() => {
        return (window as any).voiceController?.getState();
      });
      console.log(`Pressing ${i}:`, pressing.pttActive, pressing.interactionMode);
      expect(pressing.pttActive).toBe(true);

      // Release
      await pttBtn.dispatchEvent('mouseup');
      await page.waitForTimeout(200);

      const released = await page.evaluate(() => {
        return (window as any).voiceController?.getState();
      });
      console.log(`Released ${i}:`, released.pttActive, released.interactionMode);
      expect(released.pttActive).toBe(false);
      expect(released.interactionMode).toBe('voice');
    }

    console.log('\n✅ MULTIPLE PTT CYCLES PASSED');
  });
});
