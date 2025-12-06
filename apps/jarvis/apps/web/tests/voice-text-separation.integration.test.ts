/**
 * Voice/Text Separation Integration Tests
 * Tests the complete interaction flow between controllers and state machine
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { VoiceController } from '../lib/voice-controller';
import { TextChannelController } from '../lib/text-channel-controller';
import { eventBus } from '../lib/event-bus';

describe('Voice/Text Separation Integration', () => {
  let voiceController: VoiceController;
  let textController: TextChannelController;
  let mockSession: any;
  let voiceListener: any;

  beforeEach(async () => {
    // Clear event bus BEFORE initializing controllers
    eventBus.clear();

    // Setup mock VoiceController environment
    (global.navigator as any).mediaDevices = {
      getUserMedia: vi.fn().mockResolvedValue({
        getTracks: () => [{ stop: vi.fn(), enabled: true }]
      } as any)
    };

    voiceController = new VoiceController();
    voiceListener = vi.fn();
    voiceController.addListener(voiceListener);
    // No async initialize needed anymore

    textController = new TextChannelController({
      autoConnect: false,
      maxRetries: 2
    });
    await textController.initialize();

    // Mock session
    mockSession = {
      sendMessage: vi.fn().mockResolvedValue(undefined),
      sendAudio: vi.fn(),
    };

    // Wire controllers together
    textController.setSession(mockSession);
    textController.setVoiceController(voiceController);
    voiceController.setSession(mockSession);
  });

  describe('PTT voice flow', () => {
    it('should process voice transcripts when armed', () => {
      // Simulate PTT press
      voiceController.startPTT();

      // Simulate partial transcript (like user is speaking)
      voiceController.handleTranscript('Hello', false);

      expect(voiceListener).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'transcript',
          text: 'Hello',
          isFinal: false
        })
      );
    });

    it('should allow final transcript after PTT release', () => {
      // Simulate PTT press and release
      voiceController.startPTT();
      voiceController.handleTranscript('Hello wor', false); // Partial
      voiceController.stopPTT();

      // OpenAI sends final transcript AFTER release
      voiceController.handleTranscript('Hello world', true); // Final

      // Check last call
      const lastCall = voiceListener.mock.calls[voiceListener.mock.calls.length - 1][0];
      expect(lastCall).toEqual(
        expect.objectContaining({
          type: 'transcript',
          text: 'Hello world',
          isFinal: true
        })
      );
    });

    it('should only receive transcripts when track is enabled', () => {
      voiceController.startPTT();
      voiceController.handleTranscript('Hello', false);
      voiceController.stopPTT();

      // Should only have the first transcript from when track was enabled
      const transcripts = voiceListener.mock.calls.filter((call: any[]) => call[0].type === 'transcript');
      expect(transcripts).toHaveLength(1);
    });
  });

  describe('Text mode isolation', () => {
    it('should mute voice when sending text', async () => {
      // Start in voice mode with PTT active
      voiceController.startPTT();
      expect(voiceController.getState().pttActive).toBe(true);

      // Send text message
      await textController.sendText('Hello');

      // Voice should be muted and in text mode
      expect(voiceController.getState().pttActive).toBe(false);
      expect(voiceController.isTextMode()).toBe(true);
    });

    it('should not receive transcripts in text mode', async () => {
      // Send text message (switches to text mode, track disabled)
      await textController.sendText('Hello');

      // No transcripts received
      expect(voiceListener).not.toHaveBeenCalledWith(
        expect.objectContaining({ type: 'transcript' })
      );
    });

    it('should handle clean mode transitions', async () => {
      // Send text message (track disabled)
      await textController.sendText('Hello');

      // Switch back to voice
      voiceController.transitionToVoice({ handsFree: false });

      // Next transcript should be fresh
      voiceController.handleTranscript('Fresh speech', false);

      expect(voiceListener).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'transcript',
          text: 'Fresh speech'
        })
      );
    });
  });

  describe('Hands-free mode', () => {
    it('should enable voice mode when enabling hands-free', () => {
      voiceController.setHandsFree(true);

      expect(voiceController.getState().handsFree).toBe(true);
      expect(voiceController.getState().interactionMode).toBe('voice');
    });

    it('should process transcripts continuously in hands-free', () => {
      // Enable hands-free
      voiceController.setHandsFree(true);
      voiceListener.mockClear();

      // Should process transcripts without explicit arm
      voiceController.handleTranscript('Continuous 1', false);
      voiceController.handleTranscript('Continuous 2', false);

      const transcripts = voiceListener.mock.calls.filter((call: any[]) => call[0].type === 'transcript');
      expect(transcripts).toHaveLength(2);
    });

    it('should deactivate when disabling hands-free', () => {
      voiceController.setHandsFree(true);
      voiceController.handleTranscript('First', false);

      voiceController.setHandsFree(false);
      voiceListener.mockClear();

      // Check state - should be back to PTT mode
      expect(voiceController.getState().active).toBe(false);
      expect(voiceController.getState().mode).toBe('ptt');
    });
  });

  describe('State machine synchronization', () => {
    it('should keep voice controller in sync via events', async () => {
      // Transition to voice
      voiceController.transitionToVoice({ handsFree: false });
      expect(voiceController.isVoiceMode()).toBe(true);

      // Transition to text
      voiceController.transitionToText();
      expect(voiceController.isTextMode()).toBe(true);
      expect(voiceController.getState().active).toBe(false);
    });

    it('should handle rapid mode switches', async () => {
      // Voice → PTT → transcript
      voiceController.startPTT();
      voiceController.handleTranscript('Voice 1', false);
      voiceController.stopPTT();

      // Text
      voiceController.transitionToText();

      // Back to voice
      voiceController.transitionToVoice({ handsFree: false });
      voiceController.startPTT();
      voiceController.handleTranscript('Voice 2', false);

      const transcripts = voiceListener.mock.calls.filter((call: any[]) => call[0].type === 'transcript');
      expect(transcripts).toHaveLength(2);
      expect(transcripts[0][0].text).toBe('Voice 1');
      expect(transcripts[1][0].text).toBe('Voice 2');
    });
  });

  describe('Critical edge cases', () => {
    it('should handle PTT release with delayed final transcript', () => {
      // User presses PTT
      voiceController.startPTT();
      voiceController.handleTranscript('Hello wor', false); // Partial

      // User releases PTT
      voiceController.stopPTT();
      expect(voiceController.getState().pttActive).toBe(false);

      // OpenAI sends final transcript after 50ms delay (typical)
      voiceController.handleTranscript('Hello world', true); // Final

      // Should have both partial and final
      const transcripts = voiceListener.mock.calls.filter((call: any[]) => call[0].type === 'transcript');
      expect(transcripts).toHaveLength(2);
      expect(transcripts[1][0].isFinal).toBe(true);
    });

    it('should not replay ambient noise on next PTT', () => {
      // First PTT interaction
      voiceController.startPTT();
      voiceController.handleTranscript('First speech', false);
      voiceController.stopPTT();

      // Second PTT interaction
      voiceController.startPTT();
      voiceController.handleTranscript('Second speech', false);

      // Should only have the 2 real speeches
      const transcripts = voiceListener.mock.calls.filter((call: any[]) => call[0].type === 'transcript');
      expect(transcripts).toHaveLength(2);
      expect(transcripts[0][0].text).toBe('First speech');
      expect(transcripts[1][0].text).toBe('Second speech');
    });

    it('should handle text send without background transcripts', async () => {
      // Send text message
      await textController.sendText('Text message');

      // No transcripts received
      expect(voiceListener).not.toHaveBeenCalledWith(
        expect.objectContaining({ type: 'transcript' })
      );
      expect(mockSession.sendMessage).toHaveBeenCalledWith('Text message');
    });

    it('should handle hands-free toggle from text mode', () => {
      // Switch to text mode
      voiceController.transitionToText();
      expect(voiceController.isTextMode()).toBe(true);

      // Try to enable hands-free (should warn but not crash)
      const consoleSpy = vi.spyOn(console, 'warn');
      voiceController.setHandsFree(true);

      expect(consoleSpy).toHaveBeenCalled();
      expect(voiceController.isTextMode()).toBe(true);
      expect(voiceController.getState().handsFree).toBe(false);
    });

    it('should transition to voice before enabling hands-free from text', () => {
      voiceController.transitionToText();

      // Proper way: transition to voice first
      voiceController.transitionToVoice({ handsFree: true });
      voiceController.setHandsFree(true);

      expect(voiceController.isVoiceMode()).toBe(true);
      expect(voiceController.getState().handsFree).toBe(true);
    });
  });

  describe('Error handling', () => {
    it('should handle voice controller errors gracefully', async () => {
      const consoleSpy = vi.spyOn(console, 'warn');
      voiceController.setSession(null); // Ensure no session

      // Force clear internal stream to trigger warning
      (voiceController as any).micStream = null;

      voiceController.startPTT();

      expect(consoleSpy).toHaveBeenCalled();
      expect(consoleSpy.mock.calls[0][0]).toContain('No session available');
    });
  });

  describe('Performance and state consistency', () => {
    it('should handle rapid state changes without race conditions', async () => {
      // Rapid PTT cycles
      for (let i = 0; i < 10; i++) {
        voiceController.startPTT();
        voiceController.handleTranscript(`Message ${i}`, false);
        voiceController.stopPTT();
      }

      // Should have exactly 10 transcripts (all during PTT active windows)
      const transcripts = voiceListener.mock.calls.filter((call: any[]) => call[0].type === 'transcript');
      expect(transcripts).toHaveLength(10);
    });

    it('should maintain state consistency across multiple transitions', () => {
      // Complex sequence
      voiceController.transitionToVoice({ handsFree: false });
      expect(voiceController.isVoiceMode()).toBe(true);

      voiceController.startPTT();
      expect(voiceController.getState().pttActive).toBe(true);

      voiceController.stopPTT();
      expect(voiceController.getState().pttActive).toBe(false);

      voiceController.transitionToText();
      expect(voiceController.isTextMode()).toBe(true);

      voiceController.transitionToVoice({ handsFree: true });
      expect(voiceController.getState().handsFree).toBe(true);

      // State should be consistent throughout
      const state = voiceController.getState();
      expect(state.interactionMode).toBe('voice');
      expect(state.handsFree).toBe(true);
    });
  });

  describe('Real-world scenarios', () => {
    it('Scenario: User types message with TV playing in background', async () => {
      // User types and sends text
      await textController.sendText('What is the weather?');

      // No transcripts should have been received
      expect(voiceListener).not.toHaveBeenCalledWith(
        expect.objectContaining({ type: 'transcript' })
      );
      expect(mockSession.sendMessage).toHaveBeenCalledWith('What is the weather?');
    });

    it('Scenario: User switches from text to PTT voice', async () => {
      // Send text message
      await textController.sendText('First message');
      expect(voiceController.isTextMode()).toBe(true);

      // User presses PTT (transitions back to voice)
      voiceController.transitionToVoice({ handsFree: false });
      voiceController.handleTranscript('Voice message', false);

      // Should process voice transcript
      const transcripts = voiceListener.mock.calls.filter((call: any[]) => call[0].type === 'transcript');
      expect(transcripts).toHaveLength(1);
    });

    it('Scenario: User enables hands-free from text mode', async () => {
      // Send text message
      await textController.sendText('Text message');
      expect(voiceController.isTextMode()).toBe(true);

      // Transition to voice mode first (UI handler does this)
      voiceController.transitionToVoice({ handsFree: true });
      voiceController.setHandsFree(true);

      // Should now process continuous transcripts
      voiceController.handleTranscript('Hands-free speech', false);

      const transcripts = voiceListener.mock.calls.filter((call: any[]) => call[0].type === 'transcript');
      expect(transcripts).toHaveLength(1);
      expect(voiceController.getState().handsFree).toBe(true);
    });

    it('Scenario: Multiple PTT presses with proper track gating', () => {
      // First PTT: User speaks
      voiceController.startPTT();
      voiceController.handleTranscript('Check my calendar', false);
      voiceController.handleTranscript('Check my calendar', true); // Final
      voiceController.stopPTT();

      // Second PTT: User speaks again
      voiceController.startPTT();
      voiceController.handleTranscript('What time is it', false);
      voiceController.handleTranscript('What time is it', true); // Final
      voiceController.stopPTT();

      // Should have exactly 4 transcripts (2 partial + 2 final)
      const transcripts = voiceListener.mock.calls.filter((call: any[]) => call[0].type === 'transcript');
      expect(transcripts).toHaveLength(4);
      expect(transcripts[0][0].text).toContain('Check my calendar');
      expect(transcripts[2][0].text).toContain('What time is it');
    });

    it('Scenario: Rapid voice/text switching', async () => {
      // Voice
      voiceController.startPTT();
      expect(voiceController.getState().pttActive).toBe(true);
      voiceController.handleTranscript('Voice 1', false);
      voiceController.stopPTT();
      expect(voiceController.getState().pttActive).toBe(false);

      // Text
      await textController.sendText('Text 1');
      expect(voiceController.isTextMode()).toBe(true);

      // Voice
      voiceController.transitionToVoice({ handsFree: false });
      voiceController.startPTT();
      expect(voiceController.getState().pttActive).toBe(true);
      voiceController.handleTranscript('Voice 2', false);
      voiceController.stopPTT();
      expect(voiceController.getState().pttActive).toBe(false);

      // Text
      await textController.sendText('Text 2');

      // Should have 2 voice transcripts
      const transcripts = voiceListener.mock.calls.filter((call: any[]) => call[0].type === 'transcript');
      expect(transcripts).toHaveLength(2);
      // Should have 2 text sends
      expect(mockSession.sendMessage).toHaveBeenCalledTimes(2);
    });
  });

  describe('Event bus communication', () => {
    it('should emit all expected events for voice flow', () => {
      // In the new architecture, VoiceController emits events to listeners directly.
      // It relies on TextChannelController or UI logic to emit 'sending' / 'sent' for text.
      // For voice, we verify that listeners are notified.

      voiceController.startPTT();
      voiceController.handleTranscript('Test', false);
      voiceController.stopPTT();

      expect(voiceListener).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'stateChange' })
      );
      expect(voiceListener).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'transcript' })
      );
    });

    it('should emit all expected events for text flow', async () => {
      const events: string[] = [];

      eventBus.on('text_channel:sending', () => events.push('sending'));
      eventBus.on('text_channel:sent', () => events.push('sent'));
      // state:changed is emitted by transitionToText
      // eventBus.on('state:changed', () => events.push('state_changed'));

      await textController.sendText('Hello');

      expect(events).toContain('sending');
      expect(events).toContain('sent');
    });
  });
});
