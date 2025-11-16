/**
 * Feedback System Module
 * Handles haptic and audio feedback for user interactions
 */

import { logger } from '@jarvis/core';
import { CONFIG, loadFeedbackPreferences, saveFeedbackPreferences, type FeedbackPreferences } from './config';

/**
 * Audio feedback system using Web Audio API
 */
export class AudioFeedback {
  private audioContext: AudioContext | null = null;
  private enabled: boolean;
  private supported: boolean;

  constructor(enabled: boolean) {
    this.enabled = enabled;
    // Check if Web Audio API is supported
    this.supported = !!(window.AudioContext || (window as any).webkitAudioContext);
  }

  private getContext(): AudioContext | null {
    if (!this.supported) return null;

    if (!this.audioContext) {
      try {
        this.audioContext = new (window.AudioContext || (window as any).webkitAudioContext)();
      } catch (error) {
        logger.warn('Web Audio API not supported', error);
        this.supported = false;
        return null;
      }
    }
    return this.audioContext;
  }

  /**
   * Resume audio context (Safari autoplay fix) - must be called from user gesture
   */
  async resumeContext(): Promise<void> {
    if (!this.supported) return;

    const ctx = this.getContext();
    if (ctx?.state === 'suspended') {
      try {
        await ctx.resume();
      } catch (error) {
        logger.warn('Failed to resume audio context', error);
      }
    }
  }

  /**
   * Set enabled state
   */
  setEnabled(enabled: boolean): void {
    this.enabled = enabled;
  }

  /**
   * Play a tone
   */
  playTone(frequency: number, duration: number, volume: number = 0.1): void {
    if (!this.enabled || !this.supported) return;

    const ctx = this.getContext();
    if (!ctx) return;

    try {
      const oscillator = ctx.createOscillator();
      const gainNode = ctx.createGain();

      oscillator.connect(gainNode);
      gainNode.connect(ctx.destination);

      oscillator.frequency.value = frequency;
      oscillator.type = 'sine';

      // Envelope to prevent clicks
      const now = ctx.currentTime;
      gainNode.gain.setValueAtTime(0, now);
      gainNode.gain.linearRampToValueAtTime(volume, now + 0.01);
      gainNode.gain.exponentialRampToValueAtTime(0.001, now + duration / 1000);

      oscillator.start(now);
      oscillator.stop(now + duration / 1000);
    } catch (error) {
      // Silently fail if audio playback fails
    }
  }

  /**
   * Cleanup resources
   */
  cleanup(): void {
    if (this.audioContext) {
      try {
        this.audioContext.close();
      } catch (error) {
        // Ignore cleanup errors
      }
      this.audioContext = null;
    }
  }
}

/**
 * Feedback system that manages both haptic and audio feedback
 */
export class FeedbackSystem {
  private preferences: FeedbackPreferences;
  private audioFeedback: AudioFeedback;

  constructor() {
    this.preferences = loadFeedbackPreferences();
    this.audioFeedback = new AudioFeedback(this.preferences.audio);
  }

  /**
   * Initialize the feedback system (call from user gesture for audio)
   */
  async initialize(): Promise<void> {
    await this.audioFeedback.resumeContext();
  }

  /**
   * Trigger haptic feedback
   */
  triggerHaptic(pattern: number | number[]): void {
    if (!this.preferences.haptics) return;
    if (!('vibrate' in navigator)) return;

    try {
      navigator.vibrate(pattern);
    } catch (error) {
      // Silently fail if vibration not supported
    }
  }

  /**
   * Play audio tone
   */
  playTone(frequency: number, duration: number, volume: number = 0.1): void {
    this.audioFeedback.playTone(frequency, duration, volume);
  }

  /**
   * Trigger feedback for connection event
   */
  onConnect(): void {
    this.triggerHaptic(CONFIG.FEEDBACK.HAPTIC_PATTERNS.CONNECT);
    const tone = CONFIG.FEEDBACK.AUDIO_TONES.CONNECT;
    this.playTone(tone.frequency, tone.duration);
  }

  /**
   * Trigger feedback for disconnection event
   */
  onDisconnect(): void {
    this.triggerHaptic(CONFIG.FEEDBACK.HAPTIC_PATTERNS.DISCONNECT);
    const tone = CONFIG.FEEDBACK.AUDIO_TONES.DISCONNECT;
    this.playTone(tone.frequency, tone.duration);
  }

  /**
   * Trigger feedback for start speaking event
   */
  onStartSpeaking(): void {
    this.triggerHaptic(CONFIG.FEEDBACK.HAPTIC_PATTERNS.START_SPEAKING);
    const tone = CONFIG.FEEDBACK.AUDIO_TONES.START_SPEAKING;
    this.playTone(tone.frequency, tone.duration);
  }

  /**
   * Trigger feedback for stop speaking event
   */
  onStopSpeaking(): void {
    this.triggerHaptic(CONFIG.FEEDBACK.HAPTIC_PATTERNS.STOP_SPEAKING);
    const tone = CONFIG.FEEDBACK.AUDIO_TONES.STOP_SPEAKING;
    this.playTone(tone.frequency, tone.duration);
  }

  /**
   * Trigger feedback for error event
   */
  onError(): void {
    this.triggerHaptic(CONFIG.FEEDBACK.HAPTIC_PATTERNS.ERROR);
    // No audio tone for errors to avoid being annoying
  }

  /**
   * Update preferences
   */
  updatePreferences(prefs: FeedbackPreferences): void {
    this.preferences = prefs;
    this.audioFeedback.setEnabled(prefs.audio);
    saveFeedbackPreferences(prefs);
  }

  /**
   * Get current preferences
   */
  getPreferences(): FeedbackPreferences {
    return { ...this.preferences };
  }

  /**
   * Cleanup resources
   */
  cleanup(): void {
    this.audioFeedback.cleanup();
  }
}

// Export singleton instance
export const feedbackSystem = new FeedbackSystem();