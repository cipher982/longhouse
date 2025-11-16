/**
 * Voice Manager Module
 * Handles all voice-related functionality including PTT, VAD, and transcription
 */

import { logger } from '@jarvis/core';
import { VoiceButtonState } from './config';
import { stateManager } from './state-manager';
import { feedbackSystem } from './feedback-system';
import type { RadialVisualizer } from './radial-visualizer';

/**
 * Voice manager configuration
 */
export interface VoiceManagerConfig {
  onTranscript?: (text: string, isFinal: boolean) => void;
  onVADStateChange?: (active: boolean) => void;
  onPTTPress?: () => void;
  onPTTRelease?: () => void;
}

/**
 * Voice Manager class
 */
export class VoiceManager {
  private config: VoiceManagerConfig;
  private visualizer: RadialVisualizer | null = null;
  private pttActive = false;
  private vadActive = false;
  private pendingTranscript = '';

  constructor(config: VoiceManagerConfig = {}) {
    this.config = config;
  }

  /**
   * Initialize voice manager with visualizer
   */
  initialize(visualizer: RadialVisualizer): void {
    this.visualizer = visualizer;
  }

  /**
   * Handle PTT button press
   */
  handlePTTPress(): void {
    const state = stateManager.getState();

    // Only proceed if connected and ready
    if (!state.session || !stateManager.isReady()) {
      logger.info('Cannot start PTT - not ready');
      return;
    }

    // Check if we have the controllers
    const { voiceChannelController, interactionStateMachine } = state;
    if (!voiceChannelController || !interactionStateMachine) {
      logger.warn('Voice controllers not initialized');
      return;
    }

    this.pttActive = true;

    // Transition to voice mode and arm the channel
    interactionStateMachine.transitionToVoice();
    voiceChannelController.arm();

    // Update state
    stateManager.setVoiceButtonState(VoiceButtonState.SPEAKING);

    // Start visualizer
    this.visualizer?.setSpeaking(true);

    // Feedback
    feedbackSystem.onStartSpeaking();

    // Notify callback
    this.config.onPTTPress?.();

    logger.info('PTT pressed - voice armed');
  }

  /**
   * Handle PTT button release
   */
  handlePTTRelease(): void {
    if (!this.pttActive) return;

    this.pttActive = false;

    const state = stateManager.getState();
    const { voiceChannelController } = state;

    if (voiceChannelController) {
      // Check if hands-free is enabled
      const handsFreeToggle = document.getElementById('handsFreeToggle') as HTMLInputElement;
      if (!handsFreeToggle?.checked) {
        voiceChannelController.mute();
      }
    }

    // Update state if not in hands-free
    if (stateManager.isSpeaking()) {
      stateManager.setVoiceButtonState(VoiceButtonState.READY);
    }

    // Stop visualizer
    this.visualizer?.setSpeaking(false);

    // Feedback
    feedbackSystem.onStopSpeaking();

    // Notify callback
    this.config.onPTTRelease?.();

    logger.info('PTT released - voice muted');
  }

  /**
   * Handle VAD (Voice Activity Detection) state change
   */
  handleVADStateChange(active: boolean): void {
    const state = stateManager.getState();

    // Only process if we're in the right state
    if (!state.session || !stateManager.isConnected()) {
      return;
    }

    // Don't process VAD during text mode
    if (state.conversationMode === 'text') {
      return;
    }

    this.vadActive = active;

    // Update visualizer
    this.visualizer?.setSpeaking(active);

    // Update state
    if (active && stateManager.isReady()) {
      stateManager.setVoiceButtonState(VoiceButtonState.SPEAKING);
      feedbackSystem.onStartSpeaking();
    } else if (!active && stateManager.isSpeaking() && !this.pttActive) {
      stateManager.setVoiceButtonState(VoiceButtonState.READY);
      feedbackSystem.onStopSpeaking();
    }

    // Notify callback
    this.config.onVADStateChange?.(active);

    logger.debug('VAD state changed:', active);
  }

  /**
   * Handle incoming transcript
   */
  handleTranscript(text: string, isFinal: boolean): void {
    const state = stateManager.getState();

    // Check if voice channel should process this
    const { voiceChannelController } = state;
    if (!voiceChannelController) {
      return;
    }

    // Let the voice channel controller handle gating
    if (!voiceChannelController.isArmed() && !isFinal) {
      logger.debug('Dropping partial transcript - voice not armed');
      return;
    }

    // Update pending text
    if (!isFinal) {
      this.pendingTranscript = text;
      stateManager.setPendingUserText(text);
    } else {
      this.pendingTranscript = '';
      stateManager.setPendingUserText('');
    }

    // Notify callback
    this.config.onTranscript?.(text, isFinal);
  }

  /**
   * Handle hands-free mode toggle
   */
  handleHandsFreeToggle(enabled: boolean): void {
    const state = stateManager.getState();
    const { voiceChannelController, interactionStateMachine } = state;

    if (!voiceChannelController || !interactionStateMachine) {
      logger.warn('Controllers not initialized for hands-free toggle');
      return;
    }

    if (enabled) {
      // Transition to voice mode and arm
      interactionStateMachine.transitionToVoice();
      voiceChannelController.arm();
      logger.info('Hands-free mode enabled - voice armed');
    } else {
      // Mute voice
      voiceChannelController.mute();
      logger.info('Hands-free mode disabled - voice muted');
    }
  }

  /**
   * Check if currently speaking
   */
  isSpeaking(): boolean {
    return this.pttActive || this.vadActive;
  }

  /**
   * Check if PTT is active
   */
  isPTTActive(): boolean {
    return this.pttActive;
  }

  /**
   * Check if VAD is active
   */
  isVADActive(): boolean {
    return this.vadActive;
  }

  /**
   * Get pending transcript
   */
  getPendingTranscript(): string {
    return this.pendingTranscript;
  }

  /**
   * Clear pending transcript
   */
  clearPendingTranscript(): void {
    this.pendingTranscript = '';
    stateManager.setPendingUserText('');
  }

  /**
   * Setup keyboard shortcuts for PTT
   */
  setupKeyboardShortcuts(): void {
    // Space bar for PTT
    document.addEventListener('keydown', (e) => {
      if (e.code === 'Space' && !e.repeat && e.target === document.body) {
        e.preventDefault();
        this.handlePTTPress();
      }
    });

    document.addEventListener('keyup', (e) => {
      if (e.code === 'Space' && e.target === document.body) {
        e.preventDefault();
        this.handlePTTRelease();
      }
    });

    // Enter key as alternative
    document.addEventListener('keydown', (e) => {
      if (e.code === 'Enter' && !e.repeat && e.target === document.body && e.ctrlKey) {
        e.preventDefault();
        this.handlePTTPress();
      }
    });

    document.addEventListener('keyup', (e) => {
      if (e.code === 'Enter' && e.target === document.body && e.ctrlKey) {
        e.preventDefault();
        this.handlePTTRelease();
      }
    });
  }

  /**
   * Setup voice button event handlers
   */
  setupVoiceButton(button: HTMLElement): void {
    // Mouse events
    button.addEventListener('mousedown', () => this.handlePTTPress());
    button.addEventListener('mouseup', () => this.handlePTTRelease());
    button.addEventListener('mouseleave', () => this.handlePTTRelease());

    // Touch events
    button.addEventListener('touchstart', (e) => {
      e.preventDefault();
      this.handlePTTPress();
    });

    button.addEventListener('touchend', (e) => {
      e.preventDefault();
      this.handlePTTRelease();
    });

    button.addEventListener('touchcancel', () => this.handlePTTRelease());
  }

  /**
   * Cleanup
   */
  cleanup(): void {
    this.pttActive = false;
    this.vadActive = false;
    this.pendingTranscript = '';
    this.visualizer = null;
  }
}

// Export singleton instance
export const voiceManager = new VoiceManager();