/**
 * Test helpers for e2e testing
 * Allows mocking OpenAI Realtime connections for Playwright tests
 */

interface MockSessionOptions {
  autoConnect?: boolean;
}

export function createMockRealtimeSession(options: MockSessionOptions = {}) {
  const eventHandlers = new Map<string, Set<Function>>();

  return {
    on(event: string, handler: Function) {
      if (!eventHandlers.has(event)) {
        eventHandlers.set(event, new Set());
      }
      eventHandlers.get(event)!.add(handler);
    },

    async connect() {
      // Simulate successful connection
      return Promise.resolve();
    },

    async disconnect() {
      return Promise.resolve();
    },

    // Test helper to emit events
    __emit(event: string, data: any) {
      const handlers = eventHandlers.get(event);
      if (handlers) {
        handlers.forEach(handler => handler(data));
      }
    }
  };
}

export function createMockMediaStream() {
  let trackEnabled = false;

  const track = {
    get enabled() { return trackEnabled; },
    set enabled(value: boolean) { trackEnabled = value; },
    stop: () => {}
  };

  return {
    getAudioTracks: () => [track],
    getTracks: () => [track]
  };
}

/**
 * Enable test mode - call this from Playwright tests
 */
export function enableTestMode() {
  (window as any).__JARVIS_TEST_MODE__ = true;
}

/**
 * Check if in test mode
 */
export function isTestMode(): boolean {
  return !!(window as any).__JARVIS_TEST_MODE__;
}

/**
 * Mock the connection for tests
 */
export async function mockConnect() {
  const { voiceController, audioController, appController } = window as any;

  if (!voiceController || !audioController || !appController) {
    throw new Error('Controllers not initialized');
  }

  // Create mock session
  const mockSession = createMockRealtimeSession();

  // Create mock mic stream
  const mockStream = createMockMediaStream();

  // Set up controllers as if connected
  voiceController.setSession(mockSession);
  voiceController.setMicrophoneStream(mockStream);
  audioController.micStream = mockStream;

  // Set voice mode for PTT
  voiceController.transitionToVoice({ handsFree: false });

  return { session: mockSession, stream: mockStream };
}

// ==========================================
// History Hydration Test Helpers
// ==========================================

/**
 * Test codeword flow for history hydration
 *
 * Usage from browser console:
 *
 * 1. First session - establish codeword:
 *    - Connect normally (click mic button)
 *    - Say: "The secret codeword is banana"
 *    - Verify Jarvis acknowledges
 *
 * 2. Verify IndexedDB storage:
 *    __jarvisTestHelpers__.checkHistory()
 *
 * 3. Refresh the page (Cmd+R / F5)
 *
 * 4. Reconnect (click mic button)
 *    - Check console for "📜 Hydrated X history items"
 *
 * 5. Test recall:
 *    - Say: "What was the codeword I mentioned?"
 *    - Jarvis should recall "banana"
 */

/**
 * Inject a test codeword turn directly into IndexedDB
 * Use this to set up the test without needing voice
 */
export async function injectTestCodeword(codeword: string = 'banana'): Promise<void> {
  const { stateManager } = window as any;
  const sessionManager = stateManager?.getState?.()?.sessionManager;

  if (!sessionManager) {
    throw new Error('SessionManager not available. Wait for app to initialize.');
  }

  const userTurn = {
    id: crypto.randomUUID(),
    timestamp: new Date(),
    userTranscript: `The secret codeword is ${codeword}`,
  };

  const assistantTurn = {
    id: crypto.randomUUID(),
    timestamp: new Date(Date.now() + 1000),
    assistantResponse: `Got it! I'll remember that the codeword is "${codeword}".`,
  };

  await sessionManager.addConversationTurn(userTurn);
  await sessionManager.addConversationTurn(assistantTurn);

  console.log(`✅ Injected test codeword: "${codeword}"`);
  console.log('Now refresh the page, connect, and ask: "What was the codeword?"');
}

/**
 * Check what history is stored in IndexedDB
 */
export async function checkHistory(): Promise<void> {
  const { stateManager } = window as any;
  const sessionManager = stateManager?.getState?.()?.sessionManager;

  if (!sessionManager) {
    console.error('SessionManager not available');
    return;
  }

  const history = await sessionManager.getConversationHistory();
  console.log(`📜 Found ${history.length} conversation turns in IndexedDB:`);

  history.forEach((turn: any, i: number) => {
    const time = new Date(turn.timestamp).toLocaleTimeString();
    if (turn.userTranscript) {
      console.log(`  [${i}] ${time} USER: "${turn.userTranscript.slice(0, 60)}..."`);
    }
    if (turn.assistantResponse || turn.assistantText) {
      const text = turn.assistantResponse || turn.assistantText;
      console.log(`  [${i}] ${time} ASST: "${text.slice(0, 60)}..."`);
    }
  });

  return history;
}

/**
 * Get the current session's history (what OpenAI Realtime sees)
 */
export function getSessionHistory(): any[] | null {
  const { stateManager } = window as any;
  const session = stateManager?.getState?.()?.session;

  if (!session) {
    console.log('No active session. Connect first.');
    return null;
  }

  const history = session.history;
  console.log(`🔮 Current Realtime session has ${history?.length ?? 0} items:`);

  history?.forEach((item: any, i: number) => {
    if (item.type === 'message') {
      const content = item.content?.[0];
      const text = content?.text || content?.transcript || '[audio]';
      console.log(`  [${i}] ${item.role}: "${text.slice(0, 60)}..."`);
    }
  });

  return history;
}

/**
 * Full test procedure - run from console after page load
 */
export function printTestInstructions(): void {
  console.log(`
╔══════════════════════════════════════════════════════════════╗
║          HISTORY HYDRATION TEST PROCEDURE                   ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Option A: Quick Test (inject codeword via console)          ║
║  ─────────────────────────────────────────────────────────  ║
║  1. Run: __jarvisTestHelpers__.injectTestCodeword('banana')  ║
║  2. Refresh the page                                         ║
║  3. Click mic to connect                                     ║
║  4. Look for "📜 Hydrated X history items" in console        ║
║  5. Ask: "What was the codeword I told you?"                 ║
║  6. Jarvis should say "banana"                               ║
║                                                              ║
║  Option B: Full Voice Test                                   ║
║  ─────────────────────────────────────────────────────────  ║
║  1. Click mic to connect                                     ║
║  2. Say: "The secret codeword is pineapple"                  ║
║  3. Wait for Jarvis to acknowledge                           ║
║  4. Run: __jarvisTestHelpers__.checkHistory()                ║
║  5. Refresh the page                                         ║
║  6. Click mic to reconnect                                   ║
║  7. Say: "What was the codeword?"                            ║
║  8. Jarvis should say "pineapple"                            ║
║                                                              ║
║  Debug Commands:                                             ║
║  ─────────────────────────────────────────────────────────  ║
║  __jarvisTestHelpers__.checkHistory()      - IndexedDB turns ║
║  __jarvisTestHelpers__.getSessionHistory() - Realtime items  ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
  `);
}

// Expose to window for Playwright
if (typeof window !== 'undefined') {
  (window as any).__jarvisTestHelpers__ = {
    enableTestMode,
    isTestMode,
    mockConnect,
    createMockRealtimeSession,
    createMockMediaStream,
    // History hydration test helpers
    injectTestCodeword,
    checkHistory,
    getSessionHistory,
    printTestInstructions,
  };

  // Print instructions on load for convenience
  console.log('💡 History hydration test helpers available. Run: __jarvisTestHelpers__.printTestInstructions()');
}
