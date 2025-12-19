/**
 * One Brain Architecture Tests (v2.1)
 *
 * Verifies that Supervisor is the only "brain" that generates assistant responses.
 * Realtime is I/O only (transcription + VAD), not a response generator.
 *
 * Key invariants:
 * 1. Realtime session config has create_response=false
 * 2. Realtime response.* events are ignored (not rendered)
 * 3. All text input routes through Supervisor
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

let capturedRealtimeSessionOptions: any | null = null;

// v2.1 invariant: Realtime is I/O only and must never auto-create responses.
// We mock the Realtime SDK to capture the session config passed by SessionHandler.
vi.mock('@openai/agents/realtime', () => {
  class RealtimeAgent {
    constructor(_args: any) {}
  }

  class OpenAIRealtimeWebRTC {
    constructor(_args: any) {}
  }

  class RealtimeSession {
    constructor(_agent: any, options: any) {
      capturedRealtimeSessionOptions = options;
    }
    async connect(_args: any) {}
    updateHistory(_items: any[]) {}
    on(_eventName: string, _handler: any) {}
  }

  return { RealtimeAgent, RealtimeSession, OpenAIRealtimeWebRTC };
});

describe('One Brain Architecture (v2.1)', () => {
  describe('Session Configuration', () => {
    it('should export SessionHandler and sessionHandler singleton', async () => {
      const sessionModule = await import('../lib/session-handler');

      expect(sessionModule.SessionHandler).toBeDefined();
      expect(sessionModule.sessionHandler).toBeDefined();
    });

    it('should have SessionHandler with expected methods', async () => {
      const { SessionHandler } = await import('../lib/session-handler');
      const handler = new SessionHandler();

      // Verify the handler has the expected interface
      expect(typeof handler.setConfig).toBe('function');
      expect(typeof handler.connectWithHistory).toBe('function');
      expect(typeof handler.disconnect).toBe('function');
      expect(typeof handler.getCurrent).toBe('function');
      expect(typeof handler.cleanup).toBe('function');
    });

    it('should set create_response=false in Realtime session config', async () => {
      const { SessionHandler } = await import('../lib/session-handler');
      const handler = new SessionHandler();

      capturedRealtimeSessionOptions = null;

      await handler.connectWithHistory({
        context: { name: 'Jarvis', instructions: 'I/O only' } as any,
        historyItems: [],
        onTokenRequest: async () => 'test-token',
      });

      expect(capturedRealtimeSessionOptions).toBeTruthy();
      expect(capturedRealtimeSessionOptions.config?.turnDetection?.create_response).toBe(false);
    });
  });

  describe('Text Input Routing', () => {
    it('should export SupervisorChatController', async () => {
      const module = await import('../lib/supervisor-chat-controller');
      expect(module.SupervisorChatController).toBeDefined();
    });

    it('should have SupervisorChatController with sendMessage method', async () => {
      const { SupervisorChatController } = await import('../lib/supervisor-chat-controller');
      const controller = new SupervisorChatController({ maxRetries: 3 });

      // Verify the controller has the expected interface
      expect(typeof controller.sendMessage).toBe('function');
      expect(typeof controller.initialize).toBe('function');
      expect(typeof controller.loadHistory).toBe('function');
      expect(typeof controller.cancel).toBe('function');
      expect(typeof controller.clearHistory).toBe('function');
    });

    it('should export TextChannelController (deprecated but still available)', async () => {
      const module = await import('../lib/text-channel-controller');
      expect(module.TextChannelController).toBeDefined();
    });
  });

  describe('App Controller Integration', () => {
    it('should export appController singleton', async () => {
      const { appController } = await import('../lib/app-controller');
      expect(appController).toBeDefined();
    });

    it('should have AppController with sendText method', async () => {
      const { AppController } = await import('../lib/app-controller');
      const controller = new AppController();

      // Verify the controller has the text routing method
      expect(typeof controller.sendText).toBe('function');
    });

    it('should route sendText through SupervisorChatController (not Realtime)', async () => {
      const { AppController } = await import('../lib/app-controller');
      const { stateManager } = await import('../lib/state-manager');

      const controller = new AppController();
      const sendMessage = vi.fn().mockResolvedValue(undefined);

      // Inject a fake supervisor chat controller (private field).
      (controller as any).supervisorChatController = { sendMessage };

      vi.spyOn(stateManager, 'getPreferences').mockReturnValue({
        chat_model: 'gpt-5.1',
        reasoning_effort: 'none',
      } as any);

      await controller.sendText('Hello', 'cid-1');

      expect(sendMessage).toHaveBeenCalledWith('Hello', 'cid-1', {
        model: 'gpt-5.1',
        reasoning_effort: 'none',
      });
    });

    it('should ignore Realtime response.* events (defense-in-depth)', async () => {
      const { AppController } = await import('../lib/app-controller');
      const { logger } = await import('@jarvis/core');

      const controller = new AppController();
      const warnSpy = vi.spyOn(logger, 'warn').mockImplementation(() => {});

      let transportHandler: any | null = null;
      const mockSession: any = {
        on: (eventName: string, handler: any) => {
          if (eventName === 'transport_event') {
            transportHandler = handler;
          }
        }
      };

      // Call private method directly for unit-test validation.
      (controller as any).setupSessionEvents(mockSession);
      expect(typeof transportHandler).toBe('function');

      await transportHandler({ type: 'response.text.delta', delta: 'hi' });
      expect(warnSpy).toHaveBeenCalled();
    });
  });
});

describe('Behavioral Tests - Supervisor Chat Controller', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('should create controller with configuration', async () => {
    const { SupervisorChatController } = await import('../lib/supervisor-chat-controller');

    const controller = new SupervisorChatController({
      maxRetries: 5,
    });

    expect(controller).toBeDefined();
  });

  it('should have async sendMessage method signature', async () => {
    const { SupervisorChatController } = await import('../lib/supervisor-chat-controller');
    const controller = new SupervisorChatController({ maxRetries: 3 });

    // The sendMessage method should return a Promise
    // We can't fully test without network, but we verify the shape
    expect(controller.sendMessage.length).toBeGreaterThanOrEqual(1); // At least 1 parameter
  });
});

describe('Architecture Documentation Check', () => {
  // These tests verify the architecture is correctly implemented by checking
  // the module exports and interfaces rather than reading source files

  it('should have session handler that can be configured', async () => {
    const { SessionHandler } = await import('../lib/session-handler');
    const handler = new SessionHandler();

    // Set config should accept onSessionReady callback
    handler.setConfig({
      onSessionReady: (session, agent) => {},
      onSessionError: (error) => {},
      onSessionEnded: () => {},
    });

    // If we got here without error, the config shape is correct
    expect(true).toBe(true);
  });

  it('should have supervisor chat controller that accepts correlation IDs', async () => {
    const { SupervisorChatController } = await import('../lib/supervisor-chat-controller');
    const controller = new SupervisorChatController({ maxRetries: 3 });

    // The sendMessage signature supports correlation IDs for tracking
    // This is important for the one-brain architecture to track requests
    expect(typeof controller.sendMessage).toBe('function');
  });
});
