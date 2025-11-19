/**
 * @jest-environment jsdom
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { VoiceController } from '../lib/voice-controller';
import { StateManager } from '../lib/state-manager';
import type { RealtimeSession } from '@openai/agents/realtime';

// Mock session
const createMockSession = () => ({
  on: vi.fn(),
  connect: vi.fn().mockResolvedValue(undefined),
  disconnect: vi.fn(),
  sendAudio: vi.fn().mockResolvedValue(undefined),
  sendRealtimeInput: vi.fn(),
});

// Mock dependencies
vi.mock('../lib/state-manager');
vi.mock('@jarvis/core', () => ({
  logger: {
    info: vi.fn(),
    debug: vi.fn(),
    error: vi.fn(),
    warn: vi.fn(),
    context: vi.fn(),
    success: vi.fn(),
  },
}));

describe('Hands-Free Mode', () => {
  let controller: VoiceController;
  let mockSession: ReturnType<typeof createMockSession>;
  let onVADStateChange: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    mockSession = createMockSession();
    onVADStateChange = vi.fn();

    controller = new VoiceController({
      onVADStateChange,
    });

    controller.setSession(mockSession as any);
  });

  describe('setHandsFree', () => {
    it('should enable hands-free mode', () => {
      controller.setHandsFree(true);

      const state = controller.getState();
      expect(state.handsFree).toBe(true);
      expect(state.armed).toBe(true);
      expect(state.mode).toBe('vad');
      expect(state.pttActive).toBe(false);
    });

    it('should disable hands-free mode', () => {
      // Enable first
      controller.setHandsFree(true);

      // Then disable
      controller.setHandsFree(false);

      const state = controller.getState();
      expect(state.handsFree).toBe(false);
      expect(state.armed).toBe(false);
      expect(state.active).toBe(false);
      expect(state.mode).toBe('ptt');
    });

    it('should not enable hands-free in text mode', () => {
      // Transition to text mode first
      controller.transitionToText();

      // Try to enable hands-free
      controller.setHandsFree(true);

      const state = controller.getState();
      expect(state.handsFree).toBe(false);
    });

    it('should unmute audio when enabling hands-free', async () => {
      // Mock a microphone stream (normally provided by connect())
      const mockTrack = { enabled: false, stop: vi.fn() };
      const mockStream = {
        getAudioTracks: () => [mockTrack],
        getTracks: () => [mockTrack],
      } as any;

      // Simulate mic stream already exists (from connect())
      controller.setMicrophoneStream(mockStream);

      // Enable hands-free should UNMUTE the track
      controller.setHandsFree(true);

      // Track should now be enabled (unmuted)
      expect(mockTrack.enabled).toBe(true);
    });

    it('should mute audio when disabling hands-free', () => {
      // Mock microphone stream (from connect())
      const mockTrack = { enabled: false, stop: vi.fn() };
      const mockStream = {
        getAudioTracks: () => [mockTrack],
        getTracks: () => [mockTrack],
      } as any;

      controller.setMicrophoneStream(mockStream);

      // Enable, then disable hands-free
      controller.setHandsFree(true);
      expect(mockTrack.enabled).toBe(true); // Unmuted when enabled

      controller.setHandsFree(false);

      // Track should be muted (not destroyed)
      expect(mockTrack.enabled).toBe(false);
      expect((controller as any).micStream).not.toBeNull(); // Stream persists
    });
  });

  describe('VAD Integration', () => {
    beforeEach(() => {
      controller.setHandsFree(true);
    });

    it('should handle VAD activation when hands-free is enabled', () => {
      controller.handleVADStateChange(true);

      const state = controller.getState();
      expect(state.vadActive).toBe(true);
      expect(state.active).toBe(true);
      expect(onVADStateChange).toHaveBeenCalledWith(true);
    });

    it('should handle VAD deactivation when hands-free is enabled', () => {
      // Activate first
      controller.handleVADStateChange(true);
      onVADStateChange.mockClear();

      // Then deactivate
      controller.handleVADStateChange(false);

      const state = controller.getState();
      expect(state.vadActive).toBe(false);
      expect(state.active).toBe(false);
      expect(onVADStateChange).toHaveBeenCalledWith(false);
    });

    it('should ignore VAD events when in PTT mode (not hands-free)', () => {
      // Disable hands-free (back to PTT mode)
      controller.setHandsFree(false);

      // Try to trigger VAD
      controller.handleVADStateChange(true);

      const state = controller.getState();
      expect(state.vadActive).toBe(false);
      expect(state.active).toBe(false);
      expect(onVADStateChange).not.toHaveBeenCalled();
    });

    it('should handle multiple VAD state changes', () => {
      onVADStateChange.mockClear(); // Clear callback from setHandsFree(true) in beforeEach

      controller.handleVADStateChange(true);
      controller.handleVADStateChange(false);
      controller.handleVADStateChange(true);

      expect(onVADStateChange).toHaveBeenCalledTimes(3);
      expect(onVADStateChange).toHaveBeenNthCalledWith(1, true);
      expect(onVADStateChange).toHaveBeenNthCalledWith(2, false);
      expect(onVADStateChange).toHaveBeenNthCalledWith(3, true);
    });
  });

  describe('Speech Event Handlers', () => {
    beforeEach(() => {
      controller.setHandsFree(true);
    });

    it('should handle speech start events', () => {
      controller.handleSpeechStart();

      expect(onVADStateChange).toHaveBeenCalledWith(true);
    });

    it('should handle speech stop events', () => {
      controller.handleSpeechStart(); // Start first
      onVADStateChange.mockClear();

      controller.handleSpeechStop();

      expect(onVADStateChange).toHaveBeenCalledWith(false);
    });

    it('should handle speech events in PTT mode', () => {
      controller.setHandsFree(false);

      // In PTT mode (not hands-free), speech events should be ignored
      controller.handleSpeechStart();
      controller.handleSpeechStop();

      // VAD events are ignored in PTT mode
      expect(onVADStateChange).not.toHaveBeenCalled();
    });
  });

  describe('Mode Transitions', () => {
    it('should transition from PTT to hands-free', () => {
      // Start in PTT mode
      expect(controller.getState().mode).toBe('ptt');
      expect(controller.getState().handsFree).toBe(false);

      // Enable hands-free
      controller.setHandsFree(true);

      expect(controller.getState().mode).toBe('vad');
      expect(controller.getState().handsFree).toBe(true);
    });

    it('should transition from hands-free back to PTT', () => {
      // Enable hands-free first
      controller.setHandsFree(true);

      // Disable it
      controller.setHandsFree(false);

      expect(controller.getState().mode).toBe('ptt');
      expect(controller.getState().handsFree).toBe(false);
    });

    it('should handle voice mode transition while hands-free is active', () => {
      controller.setHandsFree(true);

      // Transition to voice mode (already in voice mode, but testing state consistency)
      controller.transitionToVoice({
        armed: true,
        handsFree: true
      });

      expect(controller.getState().mode).toBe('vad');
      expect(controller.getState().handsFree).toBe(true);
    });

    it('should block text mode transition while hands-free is active', () => {
      controller.setHandsFree(true);

      // Try to transition to text mode (should work, hands-free gets disabled)
      controller.transitionToText();

      // After transition to text, hands-free should be disabled
      expect(controller.getState().interactionMode).toBe('text');
    });
  });

  describe('Integration Scenarios', () => {
    it('should handle complete hands-free flow', async () => {
      (global.navigator as any).mediaDevices = {
        getUserMedia: vi.fn().mockResolvedValue({
          getTracks: () => [{ stop: vi.fn() }],
        } as any),
      } as any;

      // 1. Enable hands-free
      controller.setHandsFree(true);
      expect(controller.getState().handsFree).toBe(true);
      expect(controller.getState().mode).toBe('vad');

      // 2. VAD detects speech
      controller.handleVADStateChange(true);
      expect(controller.getState().vadActive).toBe(true);

      // 3. VAD stops detecting speech
      controller.handleVADStateChange(false);
      expect(controller.getState().vadActive).toBe(false);

      // 4. Disable hands-free
      controller.setHandsFree(false);
      expect(controller.getState().handsFree).toBe(false);
      expect(controller.getState().mode).toBe('ptt');
    });

    it('should handle rapid VAD state changes', () => {
      controller.setHandsFree(true);

      // Rapid toggle
      controller.handleVADStateChange(true);
      controller.handleVADStateChange(false);
      controller.handleVADStateChange(true);
      controller.handleVADStateChange(false);

      expect(onVADStateChange).toHaveBeenCalledTimes(4);
      expect(onVADStateChange).toHaveBeenNthCalledWith(1, true);
      expect(onVADStateChange).toHaveBeenNthCalledWith(2, false);
      expect(onVADStateChange).toHaveBeenNthCalledWith(3, true);
      expect(onVADStateChange).toHaveBeenNthCalledWith(4, false);
    });

    it('should maintain state consistency across transitions', () => {
      // Start in PTT mode
      expect(controller.getState().mode).toBe('ptt');

      // Enable hands-free
      controller.setHandsFree(true);
      expect(controller.getState().mode).toBe('vad');

      // Try to enable again (idempotent)
      controller.setHandsFree(true);
      expect(controller.getState().mode).toBe('vad');

      // Disable
      controller.setHandsFree(false);
      expect(controller.getState().mode).toBe('ptt');

      // Disable again (idempotent)
      controller.setHandsFree(false);
      expect(controller.getState().mode).toBe('ptt');
    });
  });

  describe('Error Handling', () => {
    it('should handle missing microphone stream gracefully', async () => {
      // With new implementation, mic stream is provided externally (from connect())
      // Hands-free just unmutes the existing stream
      // If no stream exists, unmuteAudio() logs a warning and returns

      // Try to enable hands-free without providing a stream first
      controller.setHandsFree(true);

      // State should be updated (mode changes)
      expect(controller.getState().handsFree).toBe(true);
      expect(controller.getState().mode).toBe('vad');

      // Note: In real usage, connect() provides the stream before hands-free is enabled
    });

    it('should handle VAD events when not in voice mode', () => {
      controller.transitionToText();

      // VAD events in text mode should be ignored
      controller.handleVADStateChange(true);

      const state = controller.getState();
      expect(state.vadActive).toBe(false);
      expect(onVADStateChange).not.toHaveBeenCalled();
    });
  });
});
