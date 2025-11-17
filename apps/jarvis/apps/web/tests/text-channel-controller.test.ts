/**
 * TextChannelController Unit Tests
 * Tests for text message sending with queue and retry logic
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TextChannelController } from '../lib/text-channel-controller';
import { VoiceControllerCompat } from '../lib/voice-controller';
import { eventBus } from '../lib/event-bus';

describe('TextChannelController', () => {
  let textController: TextChannelController;
  let voiceController: VoiceControllerCompat;
  let mockSession: any;

  beforeEach(() => {
    textController = new TextChannelController({
      autoConnect: false, // Disable auto-connect for most tests
      maxRetries: 2,
      retryDelay: 10 // Short delay for tests
    });

    voiceController = new VoiceControllerCompat();

    // Mock session
    mockSession = {
      sendMessage: vi.fn()
    } as any;

    textController.setSession(mockSession);
    textController.setVoiceController(voiceController);

    eventBus.clear();
  });

  describe('sendText()', () => {
    it('should send text message', async () => {
      await textController.sendText('Hello');

      expect(mockSession.sendMessage).toHaveBeenCalledWith('Hello');
    });

    it('should emit text_channel:sent event on success', async () => {
      const handler = vi.fn();
      eventBus.on('text_channel:sent', handler);

      await textController.sendText('Hello');

      expect(handler).toHaveBeenCalledWith({
        text: 'Hello',
        timestamp: expect.any(Number)
      });
    });

    it('should reject empty messages', async () => {
      await expect(textController.sendText('')).rejects.toThrow('Cannot send empty message');
      await expect(textController.sendText('   ')).rejects.toThrow('Cannot send empty message');
    });

    it('should trim whitespace', async () => {
      await textController.sendText('  Hello  ');

      expect(mockSession.sendMessage).toHaveBeenCalledWith('Hello');
    });

    it('should switch to text mode', async () => {
      expect(voiceController.getState().interactionMode).toBe('voice');

      await textController.sendText('Hello');

      expect(voiceController.getState().interactionMode).toBe('text');
    });

    it('should mute voice controller', async () => {
      voiceController.arm();
      expect(voiceController.isArmed()).toBe(true);

      await textController.sendText('Hello');

      expect(voiceController.isArmed()).toBe(false);
    });

    // Note: OpenAI SDK's sendMessage() is void (fire-and-forget)
    // It doesn't throw or return promises, so retry logic won't work in practice
    // Errors come through session error events instead
    // These tests are skipped since they test behavior incompatible with the real SDK
  });

  describe('auto-connect', () => {
    it('should auto-connect if enabled and no session', async () => {
      const autoConnectController = new TextChannelController({
        autoConnect: true
      });

      const mockConnect = vi.fn().mockImplementation(async () => {
        // Simulate successful connection by setting the session
        const mockConnectedSession = {
          sendMessage: vi.fn().mockResolvedValue(undefined)
        } as any;
        autoConnectController.setSession(mockConnectedSession);
      });

      autoConnectController.setConnectCallback(mockConnect);
      autoConnectController.setVoiceController(voiceController);

      // No session initially
      autoConnectController.setSession(null);

      // This should trigger auto-connect
      await autoConnectController.sendText('Hello');

      expect(mockConnect).toHaveBeenCalled();
    });

    it('should emit error if no session and auto-connect disabled', async () => {
      const errorHandler = vi.fn();
      eventBus.on('text_channel:error', errorHandler);
      textController.setSession(null);

      // sendText emits error event instead of rejecting
      await textController.sendText('Hello');

      expect(errorHandler).toHaveBeenCalled();
      expect(errorHandler.mock.calls[0][0].message).toContain(
        'No active session and auto-connect is disabled'
      );
    });
  });

  describe('queue management', () => {
    it('should queue messages', () => {
      const status = textController.getQueueStatus();

      expect(status.pending).toBe(0);
      expect(status.sending).toBe(false);
    });

    it('should process queue in order', async () => {
      const calls: string[] = [];
      mockSession.sendMessage.mockImplementation((text: string) => {
        calls.push(text);
        return Promise.resolve();
      });

      // Send multiple messages
      await Promise.all([
        textController.sendText('First'),
        textController.sendText('Second'),
        textController.sendText('Third')
      ]);

      expect(calls).toEqual(['First', 'Second', 'Third']);
    });

    it('should clear queue', async () => {
      // Start a long-running send
      mockSession.sendMessage.mockImplementation(() => new Promise(resolve => setTimeout(resolve, 100)));

      const promise = textController.sendText('Hello');

      textController.clearQueue();

      await promise;

      const status = textController.getQueueStatus();
      expect(status.pending).toBe(0);
    });
  });

  describe('dispose()', () => {
    it('should clear queue on dispose', () => {
      textController.dispose();

      const status = textController.getQueueStatus();
      expect(status.pending).toBe(0);
    });

    it('should clear session reference', async () => {
      textController.dispose();

      const errorHandler = vi.fn();
      eventBus.on('text_channel:error', errorHandler);

      // Trying to send after dispose should emit error
      await textController.sendText('Hello');

      expect(errorHandler).toHaveBeenCalled();
    });
  });
});
