import { describe, it, expect, beforeEach, vi, type Mock } from 'vitest';
import { VoiceController, type VoiceState, type VoiceEvent } from '../lib/voice-controller';

describe('VoiceController', () => {
  let controller: VoiceController;
  let listener: Mock;
  let mockSession: any;

  beforeEach(() => {
    // Create controller
    controller = new VoiceController();
    
    // Mock listener
    listener = vi.fn();
    controller.addListener(listener);

    // Create mock session
    mockSession = {
      sendAudio: vi.fn() 
    };

    // Mock getUserMedia
    global.navigator = {
      mediaDevices: {
        getUserMedia: vi.fn().mockResolvedValue({
          getTracks: () => [{
            stop: vi.fn(),
            enabled: true // Default enabled
          }]
        })
      }
    } as any;
  });

  describe('Initial State', () => {
    it('should have correct initial state', () => {
      const state = controller.getState();
      expect(state).toEqual({
        mode: 'ptt',
        interactionMode: 'voice',
        active: false,
        armed: false,
        handsFree: false,
        transcript: '',
        finalTranscript: '',
        vadActive: false,
        pttActive: false
      });
    });
  });

  describe('PTT (Push-to-Talk)', () => {
    it('should activate on PTT press with session', async () => {
      controller.setSession(mockSession);
      listener.mockClear(); // Clear initial state event
      
      controller.startPTT();

      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'stateChange',
          state: expect.objectContaining({
            active: true,
            armed: true,
            pttActive: true,
            mode: 'ptt'
          })
        })
      );
    });

    it('should still arm even without session (for testing)', () => {
      controller.startPTT();

      expect(controller.getState().armed).toBe(true);
      // Should trigger state change
      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({
           type: 'stateChange',
           state: expect.objectContaining({ armed: true })
        })
      );
    });

    it('should deactivate on PTT release', () => {
      controller.setSession(mockSession);
      controller.startPTT();
      listener.mockClear();
      
      controller.stopPTT();

      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'stateChange',
          state: expect.objectContaining({
            active: false,
            armed: false,
            pttActive: false
          })
        })
      );
    });

    it('should clear transcripts on new PTT press', () => {
      controller.setSession(mockSession);

      // Simulate having old transcript
      controller.handleTranscript('old text', false);

      // Start new PTT
      controller.startPTT();

      const state = controller.getState();
      expect(state.transcript).toBe('');
      expect(state.finalTranscript).toBe('');
    });
  });

  describe('Transcript Handling', () => {
    beforeEach(() => {
      controller.setSession(mockSession);
      listener.mockClear();
    });

    it('should accept all transcripts', () => {
      controller.handleTranscript('Hello world', false);

      // Should emit state change
      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'stateChange',
          state: expect.objectContaining({
            transcript: 'Hello world'
          })
        })
      );
      
      // Should emit transcript event
      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'transcript',
          text: 'Hello world',
          isFinal: false
        })
      );
    });

    it('should handle final transcripts', () => {
      controller.handleTranscript('Final speech', true);

      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'transcript',
          text: 'Final speech',
          isFinal: true
        })
      );
      
      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({
           type: 'stateChange',
           state: expect.objectContaining({
             finalTranscript: 'Final speech'
           })
        })
      );
    });

    it('should clear partial transcript on final', () => {
      controller.handleTranscript('Partial text', false);
      controller.handleTranscript('Final text', true);

      const state = controller.getState();
      expect(state.transcript).toBe('');
      expect(state.finalTranscript).toBe('Final text');
    });
  });

  describe('Hands-Free Mode', () => {
    it('should enable hands-free mode', () => {
      controller.setSession(mockSession);
      listener.mockClear();
      
      controller.setHandsFree(true);

      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'stateChange',
          state: expect.objectContaining({
            handsFree: true,
            armed: true,
            mode: 'vad'
          })
        })
      );
    });

    it('should accept all transcripts in hands-free mode', () => {
      controller.setSession(mockSession);
      controller.setHandsFree(true);
      listener.mockClear();

      controller.handleTranscript('Hands-free text', false);

      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'stateChange',
          state: expect.objectContaining({
            transcript: 'Hands-free text'
          })
        })
      );
    });

    it('should disable hands-free mode', () => {
      controller.setSession(mockSession);
      controller.setHandsFree(true);
      listener.mockClear();
      
      controller.setHandsFree(false);

      expect(listener).toHaveBeenCalledWith(
         expect.objectContaining({
           type: 'stateChange',
           state: expect.objectContaining({
             handsFree: false,
             armed: false,
             mode: 'ptt'
           })
         })
      );
    });
  });

  describe('VAD (Voice Activity Detection)', () => {
    it('should handle VAD state in hands-free mode', () => {
      controller.setSession(mockSession);
      controller.setHandsFree(true);
      listener.mockClear();

      controller.handleVADStateChange(true);

      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'stateChange',
          state: expect.objectContaining({
            vadActive: true,
            active: true
          })
        })
      );
      
      expect(listener).toHaveBeenCalledWith(
         expect.objectContaining({
           type: 'vadStateChange',
           active: true
         })
      );
    });

    it('should ignore VAD when in PTT mode', () => {
      controller.setSession(mockSession);
      // In PTT mode, not hands-free

      controller.handleVADStateChange(true);

      const state = controller.getState();
      expect(state.vadActive).toBe(false);
      expect(state.active).toBe(false);
    });

    it('should handle VAD deactivation', () => {
      controller.setSession(mockSession);
      controller.setHandsFree(true);
      controller.handleVADStateChange(true);
      listener.mockClear();
      
      controller.handleVADStateChange(false);

      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'stateChange',
          state: expect.objectContaining({
            vadActive: false,
            active: false
          })
        })
      );
    });
  });

  describe('Session Management', () => {
    it('should handle session connection', () => {
      controller.setSession(mockSession);
      expect(controller.getState().armed).toBe(false);
    });

    it('should reset state on session disconnect', () => {
      controller.setSession(mockSession);
      controller.startPTT();
      listener.mockClear();
      
      controller.setSession(null);

      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'stateChange',
          state: expect.objectContaining({
            active: false,
            armed: false
          })
        })
      );
    });
  });

  describe('VAD Configuration', () => {
    it('should provide VAD config with defaults', () => {
      const config = controller.getVADConfig();

      expect(config).toEqual({
        type: 'server_vad',
        threshold: 0.5,
        silence_duration_ms: 500,
        prefix_padding_ms: 300
      });
    });

    it('should use custom VAD config', () => {
      const customController = new VoiceController({
        vadThreshold: 0.7,
        silenceDuration: 1000,
        prefixPadding: 500
      });

      const config = customController.getVADConfig();

      expect(config).toEqual({
        type: 'server_vad',
        threshold: 0.7,
        silence_duration_ms: 1000,
        prefix_padding_ms: 500
      });
    });
  });

  describe('Cleanup', () => {
    it('should dispose resources', () => {
      controller.setSession(mockSession);
      controller.startPTT();
      controller.dispose();

      const state = controller.getState();
      expect(state).toEqual({
        mode: 'ptt',
        interactionMode: 'voice',
        active: false,
        armed: false,
        handsFree: false,
        transcript: '',
        finalTranscript: '',
        vadActive: false,
        pttActive: false
      });
    });
  });

  describe('Edge Cases', () => {
    it('should handle empty transcripts', () => {
      controller.setSession(mockSession);
      controller.startPTT();

      listener.mockClear();

      controller.handleTranscript('', false);

      // Should not trigger any state change for empty text
      expect(listener).not.toHaveBeenCalled();
    });

    it('should handle rapid PTT presses', () => {
      controller.setSession(mockSession);

      controller.startPTT();
      controller.stopPTT();
      controller.startPTT();
      controller.stopPTT();

      const state = controller.getState();
      expect(state.pttActive).toBe(false);
      expect(state.armed).toBe(false);
    });

    it('should handle mode switching', () => {
      controller.setSession(mockSession);

      // Start in PTT
      controller.startPTT();
      expect(controller.getState().mode).toBe('ptt');

      // Switch to hands-free (should override PTT)
      controller.setHandsFree(true);
      expect(controller.getState().mode).toBe('vad');
      expect(controller.getState().pttActive).toBe(false);

      // Back to PTT
      controller.setHandsFree(false);
      expect(controller.getState().mode).toBe('ptt');
    });
  });
});