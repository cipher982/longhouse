/**
 * Feedback System Module
 * Handles haptic and audio feedback for user interactions
 */

import { CONFIG } from './config';

interface IAudioFeedback {
  audioContext: AudioContext | null;
  resumeContext(): Promise<void>;
  playConnectChime(): void;
  playVoiceTick(): void;
  playErrorTone(): void;
}

class AudioFeedback implements IAudioFeedback {
  private enabled: boolean;
  public audioContext: AudioContext | null = null;
  private supported: boolean = false;

  constructor(enabled: boolean = true) {
    this.enabled = enabled;
    this.init();
  }

  private init(): void {
    if (!this.enabled) return;

    try {
      this.audioContext = new (window.AudioContext || (window as any as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)();
      this.supported = true;
    } catch (error) {
      console.warn('Audio not supported:', error);
      this.supported = false;
    }
  }

  // Resume audio context (Safari autoplay fix) - must be called from user gesture
  async resumeContext(): Promise<void> {
    if (!this.supported || !this.audioContext) return;

    if (this.audioContext.state === 'suspended') {
      try {
        await this.audioContext.resume();
        console.debug('AudioContext resumed from user gesture');
      } catch {
        // Silently fail - might not be a user gesture context
      }
    }
  }

  // Soft chime (connection success) - compatibility method
  playConnectChime(): void {
    if (!this.enabled || !this.supported) return;
    try {
      const ctx = this.audioContext;
      if (!ctx) return;

      if (ctx.state === 'suspended') {
        ctx.resume().catch(() => {});
      }

      const oscillator = ctx.createOscillator();
      const gainNode = ctx.createGain();

      oscillator.connect(gainNode);
      gainNode.connect(ctx.destination);

      oscillator.frequency.setValueAtTime(523.25, ctx.currentTime);
      oscillator.frequency.exponentialRampToValueAtTime(659.25, ctx.currentTime + 0.1);

      gainNode.gain.setValueAtTime(0.1, ctx.currentTime);
      gainNode.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.2);

      oscillator.start(ctx.currentTime);
      oscillator.stop(ctx.currentTime + 0.2);
    } catch {
      // Silently fail
    }
  }

  // Brief tick (voice detected) - compatibility method
  playVoiceTick(): void {
    if (!this.enabled || !this.supported) return;
    try {
      const ctx = this.audioContext;
      if (!ctx) return;

      if (ctx.state === 'suspended') {
        ctx.resume().catch(() => {});
      }

      const oscillator = ctx.createOscillator();
      const gainNode = ctx.createGain();

      oscillator.connect(gainNode);
      gainNode.connect(ctx.destination);

      oscillator.frequency.setValueAtTime(800, ctx.currentTime);
      gainNode.gain.setValueAtTime(0.05, ctx.currentTime);
      gainNode.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.05);

      oscillator.start(ctx.currentTime);
      oscillator.stop(ctx.currentTime + 0.05);
    } catch {
      // Silently fail
    }
  }

  // Gentle error tone - compatibility method
  playErrorTone(): void {
    if (!this.enabled || !this.supported) return;
    try {
      const ctx = this.audioContext;
      if (!ctx) return;

      if (ctx.state === 'suspended') {
        ctx.resume().catch(() => {});
      }

      const oscillator = ctx.createOscillator();
      const gainNode = ctx.createGain();

      oscillator.connect(gainNode);
      gainNode.connect(ctx.destination);

      oscillator.frequency.setValueAtTime(300, ctx.currentTime);
      oscillator.frequency.exponentialRampToValueAtTime(200, ctx.currentTime + 0.3);

      gainNode.gain.setValueAtTime(0.08, ctx.currentTime);
      gainNode.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.3);

      oscillator.start(ctx.currentTime);
      oscillator.stop(ctx.currentTime + 0.3);
    } catch {
      // Silently fail
    }
  }

  playTone(frequency: number, duration: number): void {
    if (!this.enabled || !this.audioContext) return;

    const oscillator = this.audioContext.createOscillator();
    const gainNode = this.audioContext.createGain();

    oscillator.connect(gainNode);
    gainNode.connect(this.audioContext.destination);

    oscillator.frequency.value = frequency;
    oscillator.type = 'sine';

    gainNode.gain.setValueAtTime(0.1, this.audioContext.currentTime);
    gainNode.gain.exponentialRampToValueAtTime(0.001, this.audioContext.currentTime + duration / 1000);

    oscillator.start(this.audioContext.currentTime);
    oscillator.stop(this.audioContext.currentTime + duration / 1000);
  }

  setEnabled(enabled: boolean): void {
    this.enabled = enabled;
  }
}

class HapticFeedback {
  private enabled: boolean;

  constructor(enabled: boolean = true) {
    this.enabled = enabled;
  }

  vibrate(pattern: number | number[]): void {
    if (!this.enabled) return;

    if (navigator.vibrate) {
      navigator.vibrate(pattern);
    }
  }

  setEnabled(enabled: boolean): void {
    this.enabled = enabled;
  }
}

/**
 * Feedback System class
 */
export class FeedbackSystem {
  private haptic: HapticFeedback;
  private audio: AudioFeedback;
  private initialized = false;

  constructor() {
    const prefs = {
      haptics: true,
      audio: true
    };

    this.haptic = new HapticFeedback(prefs.haptics);
    this.audio = new AudioFeedback(prefs.audio);
  }

  async initialize(): Promise<void> {
    // Initialize audio context on user interaction
    const initAudio = () => {
      if (this.audio && this.audio.audioContext?.state === 'suspended') {
        this.audio.audioContext.resume();
      }
      document.removeEventListener('click', initAudio);
      document.removeEventListener('touchstart', initAudio);
    };

    document.addEventListener('click', initAudio);
    document.addEventListener('touchstart', initAudio);

    this.initialized = true;
  }

  onConnect(): void {
    this.haptic.vibrate(CONFIG.FEEDBACK.HAPTIC_PATTERNS.CONNECT);
    this.audio.playTone(
      CONFIG.FEEDBACK.AUDIO_TONES.CONNECT.frequency,
      CONFIG.FEEDBACK.AUDIO_TONES.CONNECT.duration
    );
  }

  onDisconnect(): void {
    this.haptic.vibrate(CONFIG.FEEDBACK.HAPTIC_PATTERNS.DISCONNECT);
    this.audio.playTone(
      CONFIG.FEEDBACK.AUDIO_TONES.DISCONNECT.frequency,
      CONFIG.FEEDBACK.AUDIO_TONES.DISCONNECT.duration
    );
  }

  onStartSpeaking(): void {
    this.haptic.vibrate(CONFIG.FEEDBACK.HAPTIC_PATTERNS.START_SPEAKING);
    this.audio.playTone(
      CONFIG.FEEDBACK.AUDIO_TONES.START_SPEAKING.frequency,
      CONFIG.FEEDBACK.AUDIO_TONES.START_SPEAKING.duration
    );
  }

  onStopSpeaking(): void {
    this.haptic.vibrate(CONFIG.FEEDBACK.HAPTIC_PATTERNS.STOP_SPEAKING);
    this.audio.playTone(
      CONFIG.FEEDBACK.AUDIO_TONES.STOP_SPEAKING.frequency,
      CONFIG.FEEDBACK.AUDIO_TONES.STOP_SPEAKING.duration
    );
  }

  onError(): void {
    this.haptic.vibrate(CONFIG.FEEDBACK.HAPTIC_PATTERNS.ERROR);
  }

  setHapticEnabled(enabled: boolean): void {
    this.haptic.setEnabled(enabled);
  }

  setAudioEnabled(enabled: boolean): void {
    this.audio.setEnabled(enabled);
  }

  // Compatibility methods for legacy AudioFeedback API
  async resumeContext(): Promise<void> {
    if (this.audio && this.audio.resumeContext) {
      await this.audio.resumeContext();
    }
  }

  playConnectChime(): void {
    // Call the internal AudioFeedback's playConnectChime
    this.audio.playConnectChime();
  }

  playVoiceTick(): void {
    // Call the internal AudioFeedback's playVoiceTick
    this.audio.playVoiceTick();
  }

  playErrorTone(): void {
    // Call the internal AudioFeedback's playErrorTone
    this.audio.playErrorTone();
  }

  cleanup(): void {
    // Nothing to cleanup for now
  }
}

// Export singleton instance
export const feedbackSystem = new FeedbackSystem();
