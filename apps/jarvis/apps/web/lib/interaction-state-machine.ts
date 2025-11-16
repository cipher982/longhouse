/**
 * Interaction State Machine
 *
 * Manages the state of user interaction (voice vs text mode)
 * and provides type-safe state transitions.
 *
 * State transitions:
 * - Voice mode can be armed/disarmed for listening
 * - Voice mode can toggle hands-free on/off
 * - Can switch between voice and text modes
 *
 * Usage:
 *   const machine = new InteractionStateMachine();
 *   machine.transitionToVoice({ armed: false, handsFree: false });
 *   machine.armVoice();  // Start listening
 *   machine.muteVoice(); // Stop listening
 */

import { eventBus, type InteractionState, type VoiceInteractionState, type TextInteractionState } from './event-bus';

export class InteractionStateMachine {
  private state: InteractionState;

  constructor(initialState?: InteractionState) {
    // Default to voice mode, unarmed, no hands-free
    this.state = initialState || {
      mode: 'voice',
      armed: false,
      handsFree: false
    };
  }

  /**
   * Get the current state
   */
  getState(): InteractionState {
    return { ...this.state };
  }

  /**
   * Check if currently in voice mode
   */
  isVoiceMode(): boolean {
    return this.state.mode === 'voice';
  }

  /**
   * Check if currently in text mode
   */
  isTextMode(): boolean {
    return this.state.mode === 'text';
  }

  /**
   * Check if voice is armed (listening)
   */
  isVoiceArmed(): boolean {
    if (this.state.mode !== 'voice') return false;
    return this.state.armed;
  }

  /**
   * Check if hands-free mode is enabled
   */
  isHandsFreeEnabled(): boolean {
    if (this.state.mode !== 'voice') return false;
    return this.state.handsFree;
  }

  /**
   * Transition to voice mode
   * @param armed Whether to arm the microphone
   * @param handsFree Whether to enable hands-free mode
   */
  transitionToVoice(options: { armed: boolean; handsFree: boolean }): void {
    const from = { ...this.state };

    this.state = {
      mode: 'voice',
      armed: options.armed,
      handsFree: options.handsFree
    };

    this.emitStateChange(from, this.state);
  }

  /**
   * Transition to text mode (automatically disarms voice)
   */
  transitionToText(): void {
    const from = { ...this.state };

    this.state = {
      mode: 'text'
    };

    this.emitStateChange(from, this.state);
  }

  /**
   * Arm the voice channel (start listening)
   * Only works in voice mode
   * Note: Only emits state:changed. The VoiceChannelController listens to this
   * and emits voice_channel:armed when it actually arms.
   */
  armVoice(): boolean {
    if (this.state.mode !== 'voice') {
      console.warn('[StateMachine] Cannot arm voice in text mode');
      return false;
    }

    if (this.state.armed) {
      // Already armed
      return false;
    }

    const from = { ...this.state };
    this.state.armed = true;

    this.emitStateChange(from, this.state);
    return true;
  }

  /**
   * Mute the voice channel (stop listening)
   * Only works in voice mode
   * Note: Only emits state:changed. The VoiceChannelController listens to this
   * and emits voice_channel:muted when it actually mutes.
   */
  muteVoice(): boolean {
    if (this.state.mode !== 'voice') {
      console.warn('[StateMachine] Cannot mute voice in text mode');
      return false;
    }

    if (!this.state.armed) {
      // Already muted
      return false;
    }

    const from = { ...this.state };
    this.state.armed = false;

    this.emitStateChange(from, this.state);
    return true;
  }

  /**
   * Toggle hands-free mode
   * Only works in voice mode
   */
  toggleHandsFree(): boolean {
    if (this.state.mode !== 'voice') {
      console.warn('[StateMachine] Cannot toggle hands-free in text mode');
      return false;
    }

    const from = { ...this.state };
    this.state.handsFree = !this.state.handsFree;

    this.emitStateChange(from, this.state);
    return this.state.handsFree;
  }

  /**
   * Set hands-free mode explicitly
   */
  setHandsFree(enabled: boolean): void {
    if (this.state.mode !== 'voice') {
      console.warn('[StateMachine] Cannot set hands-free in text mode');
      return;
    }

    if (this.state.handsFree === enabled) {
      // No change needed
      return;
    }

    const from = { ...this.state };
    this.state.handsFree = enabled;

    this.emitStateChange(from, this.state);
  }

  /**
   * Emit a state change event
   */
  private emitStateChange(from: InteractionState, to: InteractionState): void {
    eventBus.emit('state:changed', {
      from,
      to,
      timestamp: Date.now()
    });

    console.log('[StateMachine] State transition:', {
      from: this.formatState(from),
      to: this.formatState(to)
    });
  }

  /**
   * Format state for logging
   */
  private formatState(state: InteractionState): string {
    if (state.mode === 'voice') {
      return `voice(armed=${state.armed}, handsFree=${state.handsFree})`;
    }
    return 'text';
  }

  /**
   * Reset to initial state
   */
  reset(): void {
    const from = { ...this.state };

    this.state = {
      mode: 'voice',
      armed: false,
      handsFree: false
    };

    this.emitStateChange(from, this.state);
  }
}
