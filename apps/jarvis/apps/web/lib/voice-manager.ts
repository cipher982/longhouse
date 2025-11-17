/**
 * Voice Manager Module
 * Handles all voice-related functionality including PTT, VAD, and transcription
 */

import { logger } from '@jarvis/core';
import { VoiceButtonState } from './config';
import { stateManager } from './state-manager';

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
  private config: VoiceManagerConfig = {};
  private visualizer: any = null;
  private pttActive = false;
  private vadActive = false;
  private pendingTranscript = '';

  /**
   * Set configuration
   */
  setConfig(config: VoiceManagerConfig): void {
    this.config = { ...this.config, ...config };
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

    this.pttActive = true;

    // Update state
    stateManager.setVoiceButtonState(VoiceButtonState.SPEAKING);

    // Start visualizer
    this.visualizer?.setSpeaking?.(true);

    // Feedback
    this.config.onPTTPress?.();

    logger.info('PTT pressed - voice armed');
  }

  /**
   * Handle PTT button release
   */
  handlePTTRelease(): void {
    if (!this.pttActive) return;

    this.pttActive = false;

    // Update state if not in hands-free
    if (stateManager.isSpeaking()) {
      stateManager.setVoiceButtonState(VoiceButtonState.READY);
    }

    // Stop visualizer
    this.visualizer?.setSpeaking?.(false);

    // Feedback
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

    this.vadActive = active;

    // Update visualizer
    this.visualizer?.setSpeaking?.(active);

    // Update state
    if (active && stateManager.isReady()) {
      stateManager.setVoiceButtonState(VoiceButtonState.SPEAKING);
    } else if (!active && stateManager.isSpeaking() && !this.pttActive) {
      stateManager.setVoiceButtonState(VoiceButtonState.READY);
    }

    // Notify callback
    this.config.onVADStateChange?.(active);

    logger.debug('VAD state changed:', active);
  }

  /**
   * Handle incoming transcript
   */
  handleTranscript(text: string, isFinal: boolean): void {
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
    if (enabled) {
      stateManager.setVoiceButtonState(VoiceButtonState.SPEAKING);
      logger.info('Hands-free mode enabled - voice armed');
    } else {
      stateManager.setVoiceButtonState(VoiceButtonState.READY);
      logger.info('Hands-free mode disabled - voice muted');
    }
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
