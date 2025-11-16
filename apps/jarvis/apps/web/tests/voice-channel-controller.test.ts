/**
 * VoiceChannelController Unit Tests
 * Tests for voice input lifecycle management
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { VoiceChannelController } from '../lib/voice-channel-controller';
import { eventBus } from '../lib/event-bus';

describe('VoiceChannelController', () => {
  let controller: VoiceChannelController;

  beforeEach(() => {
    controller = new VoiceChannelController();
    // Clear all event handlers between tests
    eventBus.clear();
  });

  describe('arm() and mute()', () => {
    it('should arm the voice channel', () => {
      expect(controller.isArmed()).toBe(false);

      controller.arm();

      expect(controller.isArmed()).toBe(true);
    });

    it('should mute the voice channel', () => {
      controller.arm();
      expect(controller.isArmed()).toBe(true);

      controller.mute();

      expect(controller.isArmed()).toBe(false);
    });

    it('should emit voice_channel:armed event', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:armed', handler);

      controller.arm();

      expect(handler).toHaveBeenCalledWith({ armed: true });
    });

    it('should emit voice_channel:muted event', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:muted', handler);
      controller.arm();

      controller.mute();

      expect(handler).toHaveBeenCalledWith({ muted: true });
    });

    it('should be idempotent (arm when already armed)', () => {
      controller.arm();
      const handler = vi.fn();
      eventBus.on('voice_channel:armed', handler);

      controller.arm(); // Second arm

      expect(handler).not.toHaveBeenCalled();
    });

    it('should be idempotent (mute when already muted)', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:muted', handler);

      controller.mute(); // Mute when already muted

      expect(handler).not.toHaveBeenCalled();
    });
  });

  describe('handleTranscript()', () => {
    it('should emit transcript when armed', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:transcript', handler);
      controller.arm();

      controller.handleTranscript('Hello world', false);

      expect(handler).toHaveBeenCalledWith({
        transcript: 'Hello world',
        isFinal: false
      });
    });

    it('should DROP partial transcripts when not armed', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:transcript', handler);

      controller.handleTranscript('Ambient noise', false);

      expect(handler).not.toHaveBeenCalled();
    });

    it('should ALLOW final transcripts even when not armed', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:transcript', handler);

      // Critical: Final transcripts arrive after PTT release
      controller.handleTranscript('Final speech', true);

      expect(handler).toHaveBeenCalledWith({
        transcript: 'Final speech',
        isFinal: true
      });
    });

    it('should emit transcripts when hands-free enabled (even if not armed)', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:transcript', handler);
      controller.setHandsFree(true);

      controller.handleTranscript('Continuous listening', false);

      expect(handler).toHaveBeenCalled();
    });

    it('should drop empty transcripts', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:transcript', handler);
      controller.arm();

      controller.handleTranscript('', false);

      expect(handler).not.toHaveBeenCalled();
    });
  });

  describe('hands-free mode', () => {
    it('should enable hands-free mode', () => {
      expect(controller.isHandsFreeEnabled()).toBe(false);

      controller.setHandsFree(true);

      expect(controller.isHandsFreeEnabled()).toBe(true);
    });

    it('should auto-arm when enabling hands-free', () => {
      expect(controller.isArmed()).toBe(false);

      controller.setHandsFree(true);

      expect(controller.isArmed()).toBe(true);
    });

    it('should disable hands-free mode', () => {
      controller.setHandsFree(true);

      controller.setHandsFree(false);

      expect(controller.isHandsFreeEnabled()).toBe(false);
    });

    it('should process transcripts in hands-free mode without explicit arm', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:transcript', handler);
      controller.setHandsFree(true);

      // Should work even though we didn't call arm() explicitly
      controller.handleTranscript('Hands-free speech', false);

      expect(handler).toHaveBeenCalled();
    });
  });

  describe('handleSpeechStart() and handleSpeechStop()', () => {
    it('should emit speaking_started when armed', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:speaking_started', handler);
      controller.arm();

      controller.handleSpeechStart();

      expect(handler).toHaveBeenCalled();
      expect(handler.mock.calls[0][0]).toHaveProperty('timestamp');
    });

    it('should NOT emit speaking_started when not armed', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:speaking_started', handler);

      controller.handleSpeechStart();

      expect(handler).not.toHaveBeenCalled();
    });

    it('should emit speaking_stopped when armed', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:speaking_stopped', handler);
      controller.arm();

      controller.handleSpeechStop();

      expect(handler).toHaveBeenCalled();
    });

    it('should emit events in hands-free mode', () => {
      const startHandler = vi.fn();
      const stopHandler = vi.fn();
      eventBus.on('voice_channel:speaking_started', startHandler);
      eventBus.on('voice_channel:speaking_stopped', stopHandler);
      controller.setHandsFree(true);

      controller.handleSpeechStart();
      controller.handleSpeechStop();

      expect(startHandler).toHaveBeenCalled();
      expect(stopHandler).toHaveBeenCalled();
    });
  });

  describe('getVADConfig()', () => {
    it('should return VAD configuration', () => {
      const config = controller.getVADConfig();

      expect(config).toEqual({
        type: 'server_vad',
        threshold: 0.5,
        silence_duration_ms: 500,
        prefix_padding_ms: 300
      });
    });

    it('should use custom configuration', () => {
      const customController = new VoiceChannelController({
        vadThreshold: 0.7,
        silenceDuration: 1000,
        prefixPadding: 500
      });

      const config = customController.getVADConfig();

      expect(config.threshold).toBe(0.7);
      expect(config.silence_duration_ms).toBe(1000);
      expect(config.prefix_padding_ms).toBe(500);
    });
  });

  describe('release()', () => {
    it('should reset armed state', () => {
      controller.arm();

      controller.release();

      expect(controller.isArmed()).toBe(false);
    });

    it('should stop microphone tracks if stream exists', async () => {
      // Mock getUserMedia
      const mockTrack = { stop: vi.fn() };
      const mockStream = { getTracks: () => [mockTrack] };

      global.navigator = {
        mediaDevices: {
          getUserMedia: vi.fn().mockResolvedValue(mockStream)
        }
      } as any;

      await controller.requestMicrophone();

      controller.release();

      expect(mockTrack.stop).toHaveBeenCalled();
      expect(controller.getMicrophoneStream()).toBe(null);
    });
  });
});
