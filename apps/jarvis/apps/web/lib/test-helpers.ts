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

  // Set armed for PTT
  voiceController.transitionToVoice({ armed: true, handsFree: false });

  return { session: mockSession, stream: mockStream };
}

// Expose to window for Playwright
if (typeof window !== 'undefined') {
  (window as any).__jarvisTestHelpers__ = {
    enableTestMode,
    isTestMode,
    mockConnect,
    createMockRealtimeSession,
    createMockMediaStream
  };
}
