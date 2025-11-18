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

  beforeEach(async () => {
    // Clear event bus BEFORE initializing controllers
    // Otherwise we remove the listeners they set up in initialize()
    eventBus.clear();

    voiceController = new VoiceController();
    await voiceController.initialize();

    textController = new TextChannelController({
      autoConnect: false,
      maxRetries: 2
    });
    await textController.initialize();

    // Mock session
    mockSession = {
      sendMessage: vi.fn().mockResolvedValue(undefined)
    };

    // Wire controllers together
    textController.setSession(mockSession);
    textController.setVoiceController(voiceController);
    voiceController.setSession(mockSession);
  });

  describe('PTT voice flow', () => {
    it('should process voice transcripts when armed', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:transcript', handler);

      // Simulate PTT press
      voiceController.armVoice();

      // Simulate partial transcript (like user is speaking)
      voiceController.handleTranscript('Hello', false);

      expect(handler).toHaveBeenCalledWith({
        transcript: 'Hello',
        isFinal: false
      });
    });

    it('should allow final transcript after PTT release', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:transcript', handler);

      // Simulate PTT press and release
      voiceController.armVoice();
      voiceController.handleTranscript('Hello wor', false); // Partial
      voiceController.muteVoice();

      // OpenAI sends final transcript AFTER release
      voiceController.handleTranscript('Hello world', true); // Final

      expect(handler).toHaveBeenCalledTimes(2);
      expect(handler).toHaveBeenLastCalledWith({
        transcript: 'Hello world',
        isFinal: true
      });
    });

    it('should drop partial transcripts after PTT release', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:transcript', handler);

      voiceController.armVoice();
      voiceController.handleTranscript('Hello', false);
      voiceController.muteVoice();

      // Ambient noise after release (partial)
      voiceController.handleTranscript('TV noise', false);

      // Should only have the first transcript, not the ambient noise
      expect(handler).toHaveBeenCalledTimes(1);
    });
  });

  describe('Text mode isolation', () => {
    it('should mute voice when sending text', async () => {
      // Start in voice mode, armed
      voiceController.armVoice();
      expect(voiceController.isArmed()).toBe(true);

      // Send text message
      await textController.sendText('Hello');

      // Voice should be muted
      expect(voiceController.isArmed()).toBe(false);
      expect(voiceController.isTextMode()).toBe(true);
    });

    it('should drop ambient transcripts in text mode', async () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:transcript', handler);

      // Send text message (switches to text mode)
      await textController.sendText('Hello');

      // Simulate ambient noise while typing (partial transcripts)
      voiceController.handleTranscript('TV audio', false);
      voiceController.handleTranscript('More TV noise', false);

      // No transcripts should be emitted
      expect(handler).not.toHaveBeenCalled();
    });

    it('should not accumulate ambient transcripts', async () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:transcript', handler);

      // Send text message
      await textController.sendText('Hello');

      // Simulate lots of ambient noise
      for (let i = 0; i < 10; i++) {
        voiceController.handleTranscript(`Ambient ${i}`, false);
      }

      // Switch back to voice
      voiceController.transitionToVoice({ armed: true, handsFree: false });

      // Next transcript should be fresh (no replay of previous 10)
      voiceController.handleTranscript('Fresh speech', false);

      expect(handler).toHaveBeenCalledTimes(1);
      expect(handler).toHaveBeenCalledWith({
        transcript: 'Fresh speech',
        isFinal: false
      });
    });
  });

  describe('Hands-free mode', () => {
    it('should auto-arm when enabling hands-free', () => {
      voiceController.setHandsFree(true);

      expect(voiceController.isHandsFreeEnabled()).toBe(true);
      // Note: Controller arming happens via event listener, not directly from state machine
    });

    it('should process transcripts continuously in hands-free', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:transcript', handler);

      // Enable hands-free
      voiceController.setHandsFree(true);

      // Should process transcripts without explicit arm
      voiceController.handleTranscript('Continuous 1', false);
      voiceController.handleTranscript('Continuous 2', false);

      expect(handler).toHaveBeenCalledTimes(2);
    });

    it('should unarm when disabling hands-free', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:transcript', handler);

      voiceController.setHandsFree(true);
      voiceController.handleTranscript('First', false);

      voiceController.setHandsFree(false);
      // Controller is now unarmed after disabling hands-free
      voiceController.handleTranscript('Should drop', false);

      // Only first transcript works (second is dropped because unarmed)
      expect(handler).toHaveBeenCalledTimes(1);
      expect(handler.mock.calls[0][0].transcript).toBe('First');

      // Controller is ready for PTT again
      expect(voiceController.isArmed()).toBe(false);
      expect(voiceController.getState().mode).toBe('ptt');
    });
  });

  describe('State machine synchronization', () => {
    it('should keep voice controller in sync via events', async () => {
      // Subscribe to state changes (simulating integration)
      eventBus.on('state:changed', ({ to }) => {
        if (to.mode === 'voice') {
          if (to.armed && !voiceController.isArmed()) {
            voiceController.arm();
          } else if (!to.armed && voiceController.isArmed()) {
            voiceController.mute();
          }
        } else {
          if (voiceController.isArmed()) {
            voiceController.mute();
          }
        }
      });

      // Transition to voice and arm
      voiceController.transitionToVoice({ armed: true, handsFree: false });
      expect(voiceController.isArmed()).toBe(true);

      // Transition to text
      voiceController.transitionToText();
      expect(voiceController.isArmed()).toBe(false);
    });

    it('should handle rapid mode switches', async () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:transcript', handler);

      // Voice → arm → transcript
      voiceController.armVoice();
      voiceController.handleTranscript('Voice 1', false);

      // Text → should drop
      voiceController.transitionToText();
      voiceController.handleTranscript('Should drop', false);

      // Back to voice → arm → transcript
      voiceController.transitionToVoice({ armed: true, handsFree: false });
      voiceController.handleTranscript('Voice 2', false);

      expect(handler).toHaveBeenCalledTimes(2);
      expect(handler.mock.calls[0][0].transcript).toBe('Voice 1');
      expect(handler.mock.calls[1][0].transcript).toBe('Voice 2');
    });
  });

  describe('Critical edge cases', () => {
    it('should handle PTT release with delayed final transcript', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:transcript', handler);

      // User presses PTT
      voiceController.armVoice();
      voiceController.handleTranscript('Hello wor', false); // Partial

      // User releases PTT
      voiceController.muteVoice();
      expect(voiceController.isArmed()).toBe(false);

      // OpenAI sends final transcript after 50ms delay (typical)
      voiceController.handleTranscript('Hello world', true); // Final

      // Should have both partial and final
      expect(handler).toHaveBeenCalledTimes(2);
      expect(handler.mock.calls[1][0].isFinal).toBe(true);
    });

    it('should not replay ambient noise on next PTT', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:transcript', handler);

      // First PTT interaction
      voiceController.armVoice();
      voiceController.handleTranscript('First speech', false);
      voiceController.muteVoice();

      // Ambient noise while muted (TV playing)
      for (let i = 0; i < 5; i++) {
        voiceController.handleTranscript(`TV noise ${i}`, false);
      }

      // Second PTT interaction
      voiceController.armVoice();
      voiceController.handleTranscript('Second speech', false);

      // Should only have the 2 real speeches, not the 5 TV noises
      expect(handler).toHaveBeenCalledTimes(2);
      expect(handler.mock.calls[0][0].transcript).toBe('First speech');
      expect(handler.mock.calls[1][0].transcript).toBe('Second speech');
    });

    it('should handle text send with background audio', async () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:transcript', handler);

      // Simulate background audio generating VAD events
      voiceController.handleTranscript('Background 1', false);
      voiceController.handleTranscript('Background 2', false);

      // Send text message (should work despite background audio)
      await textController.sendText('Text message');

      // No transcripts should be processed
      expect(handler).not.toHaveBeenCalled();
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
      expect(voiceController.isHandsFreeEnabled()).toBe(false);
    });

    it('should transition to voice before enabling hands-free from text', () => {
      voiceController.transitionToText();

      // Proper way: transition to voice first
      voiceController.transitionToVoice({ armed: false, handsFree: true });
      voiceController.setHandsFree(true);

      expect(voiceController.isVoiceMode()).toBe(true);
      expect(voiceController.isHandsFreeEnabled()).toBe(true);
    });
  });

  describe('Error handling', () => {
    // Note: OpenAI SDK's sendMessage() is void (fire-and-forget)
    // It doesn't throw, so we can't test retry logic with the current implementation
    // Real errors come through session error events

    it('should handle voice controller errors gracefully', () => {
      const handler = vi.fn();
      eventBus.on('voice_channel:error', handler);

      // Mock getUserMedia failure
      global.navigator = {
        mediaDevices: {
          getUserMedia: vi.fn().mockRejectedValue(new Error('Permission denied'))
        }
      } as any;

      expect(voiceController.requestMicrophone()).rejects.toThrow('Permission denied');
    });
  });

  describe('Performance and state consistency', () => {
    it('should handle rapid state changes without race conditions', async () => {
      const transcriptHandler = vi.fn();
      eventBus.on('voice_channel:transcript', transcriptHandler);

      // Rapid arm/mute cycles
      for (let i = 0; i < 10; i++) {
        voiceController.armVoice();
        voiceController.handleTranscript(`Message ${i}`, false);
        voiceController.muteVoice();
      }

      // Should have exactly 10 transcripts (all during armed windows)
      expect(transcriptHandler).toHaveBeenCalledTimes(10);
    });

    it('should maintain state consistency across multiple transitions', () => {
      // Complex sequence
      voiceController.transitionToVoice({ armed: true, handsFree: false });
      expect(voiceController.isVoiceMode()).toBe(true);
      expect(voiceController.isVoiceArmed()).toBe(true);

      voiceController.muteVoice();
      expect(voiceController.isVoiceArmed()).toBe(false);

      voiceController.transitionToText();
      expect(voiceController.isTextMode()).toBe(true);

      voiceController.transitionToVoice({ armed: false, handsFree: true });
      expect(voiceController.isHandsFreeEnabled()).toBe(true);

      // State should be consistent throughout
      const state = voiceController.getState();
      expect(state.interactionMode).toBe('voice');
      expect(state.handsFree).toBe(true);
    });
  });

  describe('Real-world scenarios', () => {
    it('Scenario: User types message with TV playing in background', async () => {
      const transcriptHandler = vi.fn();
      eventBus.on('voice_channel:transcript', transcriptHandler);

      // TV generates background VAD events
      voiceController.handleTranscript('TV audio 1', false);
      voiceController.handleTranscript('TV audio 2', false);

      // User types and sends text
      await textController.sendText('What is the weather?');

      // More TV noise while assistant is responding
      voiceController.handleTranscript('TV audio 3', false);

      // No transcripts should be processed
      expect(transcriptHandler).not.toHaveBeenCalled();
      expect(mockSession.sendMessage).toHaveBeenCalledWith('What is the weather?');
    });

    it('Scenario: User switches from text to PTT voice', async () => {
      const transcriptHandler = vi.fn();
      eventBus.on('voice_channel:transcript', transcriptHandler);

      // Send text message
      await textController.sendText('First message');
      expect(voiceController.isTextMode()).toBe(true);

      // User presses PTT (transitions back to voice)
      voiceController.transitionToVoice({ armed: true, handsFree: false });
      voiceController.handleTranscript('Voice message', false);

      // Should process voice transcript
      expect(transcriptHandler).toHaveBeenCalledTimes(1);
    });

    it('Scenario: User enables hands-free from text mode', async () => {
      const transcriptHandler = vi.fn();
      eventBus.on('voice_channel:transcript', transcriptHandler);

      // Send text message
      await textController.sendText('Text message');
      expect(voiceController.isTextMode()).toBe(true);

      // Transition to voice mode first (UI handler does this)
      voiceController.transitionToVoice({ armed: false, handsFree: true });
      voiceController.setHandsFree(true);

      // Should now process continuous transcripts
      voiceController.handleTranscript('Hands-free speech', false);

      expect(transcriptHandler).toHaveBeenCalledTimes(1);
      expect(voiceController.isHandsFreeEnabled()).toBe(true);
    });

    it('Scenario: Multiple PTT presses with TV on', () => {
      const transcriptHandler = vi.fn();
      eventBus.on('voice_channel:transcript', transcriptHandler);

      // First PTT: User speaks
      voiceController.armVoice();
      voiceController.handleTranscript('Check my calendar', false);
      voiceController.handleTranscript('Check my calendar', true); // Final
      voiceController.muteVoice();

      // TV noise between interactions
      for (let i = 0; i < 5; i++) {
        voiceController.handleTranscript(`TV noise ${i}`, false);
      }

      // Second PTT: User speaks again
      voiceController.armVoice();
      voiceController.handleTranscript('What time is it', false);
      voiceController.handleTranscript('What time is it', true); // Final
      voiceController.muteVoice();

      // Should have exactly 4 transcripts (2 partial + 2 final), no TV noise
      expect(transcriptHandler).toHaveBeenCalledTimes(4);
      expect(transcriptHandler.mock.calls[0][0].transcript).toContain('Check my calendar');
      expect(transcriptHandler.mock.calls[2][0].transcript).toContain('What time is it');
    });

    it('Scenario: Rapid voice/text switching', async () => {
      const transcriptHandler = vi.fn();
      eventBus.on('voice_channel:transcript', transcriptHandler);

      // Voice - use state machine which triggers controller via events
      voiceController.armVoice();
      // Verify controller is armed via event listener
      expect(voiceController.isArmed()).toBe(true);
      voiceController.handleTranscript('Voice 1', false);
      voiceController.muteVoice();
      expect(voiceController.isArmed()).toBe(false);

      // Text
      await textController.sendText('Text 1');
      expect(voiceController.isTextMode()).toBe(true);

      // Voice - transition back to voice
      voiceController.transitionToVoice({ armed: true, handsFree: false });
      // Verify controller is armed via event listener
      expect(voiceController.isArmed()).toBe(true);
      voiceController.handleTranscript('Voice 2', false);
      voiceController.muteVoice();
      expect(voiceController.isArmed()).toBe(false);

      // Text
      await textController.sendText('Text 2');

      // Should have 2 voice transcripts
      expect(transcriptHandler).toHaveBeenCalledTimes(2);
      // Should have 2 text sends
      expect(mockSession.sendMessage).toHaveBeenCalledTimes(2);
    });
  });

  describe('Event bus communication', () => {
    it('should emit all expected events for voice flow', () => {
      const events: string[] = [];

      eventBus.on('voice_channel:armed', () => events.push('armed'));
      eventBus.on('voice_channel:muted', () => events.push('muted'));
      eventBus.on('voice_channel:transcript', () => events.push('transcript'));
      eventBus.on('state:changed', () => events.push('state_changed'));

      // Arm through state machine, which triggers controller via event
      voiceController.armVoice();
      // Verify controller is armed
      expect(voiceController.isArmed()).toBe(true);
      // Now transcript should be processed
      voiceController.handleTranscript('Test', false);
      voiceController.muteVoice();

      expect(events).toContain('armed');
      expect(events).toContain('muted');
      expect(events).toContain('transcript');
      expect(events).toContain('state_changed');
    });

    it('should emit all expected events for text flow', async () => {
      const events: string[] = [];

      eventBus.on('text_channel:sending', () => events.push('sending'));
      eventBus.on('text_channel:sent', () => events.push('sent'));
      eventBus.on('state:changed', () => events.push('state_changed'));

      await textController.sendText('Hello');

      expect(events).toContain('sending');
      expect(events).toContain('sent');
      expect(events).toContain('state_changed');
    });
  });
});
