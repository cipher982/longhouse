/**
 * InteractionStateMachine Unit Tests
 * Tests for voice/text mode state transitions
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { InteractionStateMachine } from '../lib/interaction-state-machine';
import { eventBus } from '../lib/event-bus';

describe('InteractionStateMachine', () => {
  let machine: InteractionStateMachine;

  beforeEach(() => {
    machine = new InteractionStateMachine();
    eventBus.clear();
  });

  describe('initialization', () => {
    it('should start in voice mode by default', () => {
      const state = machine.getState();
      expect(state.mode).toBe('voice');
      if (state.mode === 'voice') {
        expect(state.armed).toBe(false);
        expect(state.handsFree).toBe(false);
      }
    });

    it('should accept custom initial state', () => {
      const customMachine = new InteractionStateMachine({
        mode: 'voice',
        armed: true,
        handsFree: true
      });

      const state = customMachine.getState();
      expect(state.mode).toBe('voice');
      if (state.mode === 'voice') {
        expect(state.armed).toBe(true);
        expect(state.handsFree).toBe(true);
      }
    });
  });

  describe('transitionToVoice()', () => {
    it('should transition from text to voice mode', () => {
      machine.transitionToText();

      machine.transitionToVoice({ armed: false, handsFree: false });

      expect(machine.isVoiceMode()).toBe(true);
      expect(machine.isTextMode()).toBe(false);
    });

    it('should emit state:changed event', () => {
      const handler = vi.fn();
      eventBus.on('state:changed', handler);
      machine.transitionToText();

      machine.transitionToVoice({ armed: false, handsFree: false });

      expect(handler).toHaveBeenCalledWith({
        from: { mode: 'text' },
        to: { mode: 'voice', armed: false, handsFree: false },
        timestamp: expect.any(Number)
      });
    });

    it('should set armed state', () => {
      machine.transitionToVoice({ armed: true, handsFree: false });

      expect(machine.isVoiceArmed()).toBe(true);
    });

    it('should set hands-free state', () => {
      machine.transitionToVoice({ armed: false, handsFree: true });

      expect(machine.isHandsFreeEnabled()).toBe(true);
    });
  });

  describe('transitionToText()', () => {
    it('should transition from voice to text mode', () => {
      machine.transitionToText();

      expect(machine.isTextMode()).toBe(true);
      expect(machine.isVoiceMode()).toBe(false);
    });

    it('should emit state:changed event', () => {
      const handler = vi.fn();
      eventBus.on('state:changed', handler);

      machine.transitionToText();

      expect(handler).toHaveBeenCalledWith({
        from: { mode: 'voice', armed: false, handsFree: false },
        to: { mode: 'text' },
        timestamp: expect.any(Number)
      });
    });
  });

  describe('armVoice()', () => {
    it('should arm voice channel', () => {
      const result = machine.armVoice();

      expect(result).toBe(true);
      expect(machine.isVoiceArmed()).toBe(true);
    });

    it('should NOT emit voice_channel:armed directly (only state:changed)', () => {
      const armedHandler = vi.fn();
      const stateHandler = vi.fn();
      eventBus.on('voice_channel:armed', armedHandler);
      eventBus.on('state:changed', stateHandler);

      machine.armVoice();

      // State machine should NOT emit voice_channel:armed directly
      // That's the controller's job when it receives the state change
      expect(armedHandler).not.toHaveBeenCalled();
      expect(stateHandler).toHaveBeenCalled();
    });

    it('should emit state:changed event', () => {
      const handler = vi.fn();
      eventBus.on('state:changed', handler);

      machine.armVoice();

      expect(handler).toHaveBeenCalled();
    });

    it('should fail in text mode', () => {
      machine.transitionToText();

      const result = machine.armVoice();

      expect(result).toBe(false);
      expect(machine.isVoiceArmed()).toBe(false);
    });

    it('should be idempotent (already armed)', () => {
      machine.armVoice();
      const handler = vi.fn();
      eventBus.on('state:changed', handler);

      const result = machine.armVoice();

      expect(result).toBe(false);
      expect(handler).not.toHaveBeenCalled();
    });
  });

  describe('muteVoice()', () => {
    it('should mute voice channel', () => {
      machine.armVoice();

      const result = machine.muteVoice();

      expect(result).toBe(true);
      expect(machine.isVoiceArmed()).toBe(false);
    });

    it('should NOT emit voice_channel:muted directly (only state:changed)', () => {
      machine.armVoice();
      const mutedHandler = vi.fn();
      const stateHandler = vi.fn();
      eventBus.on('voice_channel:muted', mutedHandler);
      eventBus.on('state:changed', stateHandler);

      machine.muteVoice();

      // State machine should NOT emit voice_channel:muted directly
      // That's the controller's job when it receives the state change
      expect(mutedHandler).not.toHaveBeenCalled();
      expect(stateHandler).toHaveBeenCalled();
    });

    it('should fail in text mode', () => {
      machine.transitionToText();

      const result = machine.muteVoice();

      expect(result).toBe(false);
    });

    it('should be idempotent (already muted)', () => {
      const handler = vi.fn();
      eventBus.on('state:changed', handler);

      const result = machine.muteVoice();

      expect(result).toBe(false);
      expect(handler).not.toHaveBeenCalled();
    });
  });

  describe('toggleHandsFree()', () => {
    it('should toggle hands-free mode', () => {
      const result = machine.toggleHandsFree();

      expect(result).toBe(true);
      expect(machine.isHandsFreeEnabled()).toBe(true);

      machine.toggleHandsFree();

      expect(machine.isHandsFreeEnabled()).toBe(false);
    });

    it('should emit state:changed event', () => {
      const handler = vi.fn();
      eventBus.on('state:changed', handler);

      machine.toggleHandsFree();

      expect(handler).toHaveBeenCalled();
    });

    it('should fail in text mode', () => {
      machine.transitionToText();

      const result = machine.toggleHandsFree();

      expect(result).toBe(false);
    });
  });

  describe('setHandsFree()', () => {
    it('should set hands-free mode explicitly', () => {
      machine.setHandsFree(true);

      expect(machine.isHandsFreeEnabled()).toBe(true);
    });

    it('should be idempotent', () => {
      machine.setHandsFree(true);
      const handler = vi.fn();
      eventBus.on('state:changed', handler);

      machine.setHandsFree(true); // Already true

      expect(handler).not.toHaveBeenCalled();
    });

    it('should warn in text mode', () => {
      machine.transitionToText();
      const consoleSpy = vi.spyOn(console, 'warn');

      machine.setHandsFree(true);

      expect(consoleSpy).toHaveBeenCalledWith(
        expect.stringContaining('Cannot set hands-free in text mode')
      );
    });
  });

  describe('reset()', () => {
    it('should reset to initial state', () => {
      machine.transitionToText();
      machine.transitionToVoice({ armed: true, handsFree: true });

      machine.reset();

      const state = machine.getState();
      expect(state.mode).toBe('voice');
      if (state.mode === 'voice') {
        expect(state.armed).toBe(false);
        expect(state.handsFree).toBe(false);
      }
    });

    it('should emit state:changed event', () => {
      const handler = vi.fn();
      machine.transitionToText();
      eventBus.on('state:changed', handler);

      machine.reset();

      expect(handler).toHaveBeenCalled();
    });
  });
});
