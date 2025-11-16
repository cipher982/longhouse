/**
 * VoiceChannelController - Manages voice input lifecycle
 *
 * Responsibilities:
 * - Lazy microphone access (only request when first needed)
 * - arm() / mute() / isArmed() state management
 * - Gate transcription callbacks based on armed state
 * - Emit events for voice activity and transcripts
 * - Support optional hands-free mode
 *
 * Usage:
 *   const controller = new VoiceChannelController();
 *   await controller.initialize();
 *   controller.arm();  // Start listening
 *   controller.mute(); // Stop listening
 */

import { eventBus } from './event-bus';
import type { RealtimeSession } from '@openai/agents/realtime';

export interface VoiceChannelConfig {
  // VAD configuration
  vadThreshold?: number;
  silenceDuration?: number;
  prefixPadding?: number;

  // Audio constraints
  audioConstraints?: MediaTrackConstraints;
}

export class VoiceChannelController {
  private micStream: MediaStream | null = null;
  private armed: boolean = false;
  private handsFreeEnabled: boolean = false;
  private session: RealtimeSession | null = null;
  private config: VoiceChannelConfig;

  constructor(config: VoiceChannelConfig = {}) {
    this.config = {
      vadThreshold: config.vadThreshold || 0.5,
      silenceDuration: config.silenceDuration || 500,
      prefixPadding: config.prefixPadding || 300,
      audioConstraints: config.audioConstraints || {
        echoCancellation: true,
        noiseSuppression: true,
        channelCount: 1
      }
    };
  }

  /**
   * Initialize the controller (does NOT request microphone yet)
   */
  async initialize(): Promise<void> {
    console.log('[VoiceController] Initialized');

    // Subscribe to state changes to handle arming/muting
    eventBus.on('state:changed', ({ to }) => {
      if (to.mode === 'voice') {
        // Voice mode activated
        if (to.armed && !this.armed) {
          this.arm();
        } else if (!to.armed && this.armed) {
          this.mute();
        }

        if (to.handsFree !== this.handsFreeEnabled) {
          this.setHandsFree(to.handsFree);
        }
      } else {
        // Text mode - ensure voice is muted
        if (this.armed) {
          this.mute();
        }
      }
    });
  }

  /**
   * Set the realtime session for VAD event handling
   */
  setSession(session: RealtimeSession | null): void {
    this.session = session;
  }

  /**
   * Request microphone access (lazy initialization)
   */
  async requestMicrophone(): Promise<MediaStream> {
    if (this.micStream) {
      return this.micStream;
    }

    try {
      console.log('[VoiceController] Requesting microphone access...');

      this.micStream = await navigator.mediaDevices.getUserMedia({
        audio: this.config.audioConstraints
      });

      console.log('[VoiceController] Microphone access granted');

      eventBus.emit('voice_channel:mic_ready', { stream: this.micStream });

      return this.micStream;
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : 'Unknown error';
      console.error('[VoiceController] Microphone access denied:', error);

      eventBus.emit('voice_channel:error', {
        error: error instanceof Error ? error : new Error(errorMessage),
        message: `Failed to access microphone: ${errorMessage}`
      });

      throw error;
    }
  }

  /**
   * Get the current microphone stream (or null if not requested)
   */
  getMicrophoneStream(): MediaStream | null {
    return this.micStream;
  }

  /**
   * Arm the voice channel (start listening)
   * This makes the microphone "hot" and ready to capture voice
   */
  arm(): void {
    if (this.armed) {
      console.log('[VoiceController] Already armed');
      return;
    }

    console.log('[VoiceController] Arming voice channel');
    this.armed = true;

    eventBus.emit('voice_channel:armed', { armed: true });
  }

  /**
   * Mute the voice channel (stop listening)
   */
  mute(): void {
    if (!this.armed) {
      console.log('[VoiceController] Already muted');
      return;
    }

    console.log('[VoiceController] Muting voice channel');
    this.armed = false;

    eventBus.emit('voice_channel:muted', { muted: true });
  }

  /**
   * Check if the voice channel is armed
   */
  isArmed(): boolean {
    return this.armed;
  }

  /**
   * Set hands-free mode
   * When enabled, voice stays armed continuously (no PTT required)
   * Note: Caller should ensure state machine is in voice mode before calling this
   */
  setHandsFree(enabled: boolean): void {
    if (this.handsFreeEnabled === enabled) {
      return;
    }

    console.log(`[VoiceController] Hands-free mode: ${enabled ? 'enabled' : 'disabled'}`);
    this.handsFreeEnabled = enabled;

    // Auto-arm when enabling hands-free
    // Note: This is safe because the UI handler ensures we transition to voice mode first
    if (enabled && !this.armed) {
      this.arm();
    }
  }

  /**
   * Check if hands-free mode is enabled
   */
  isHandsFreeEnabled(): boolean {
    return this.handsFreeEnabled;
  }

  /**
   * Handle VAD speech start event
   * Only processes if armed OR hands-free is enabled
   */
  handleSpeechStart(): void {
    if (!this.armed && !this.handsFreeEnabled) {
      console.log('[VoiceController] Speech start ignored (muted)');
      return;
    }

    console.log('[VoiceController] Speech started');
    eventBus.emit('voice_channel:speaking_started', { timestamp: Date.now() });
  }

  /**
   * Handle VAD speech stop event
   * Only processes if armed OR hands-free is enabled
   */
  handleSpeechStop(): void {
    if (!this.armed && !this.handsFreeEnabled) {
      console.log('[VoiceController] Speech stop ignored (muted)');
      return;
    }

    console.log('[VoiceController] Speech stopped');
    eventBus.emit('voice_channel:speaking_stopped', { timestamp: Date.now() });
  }

  /**
   * Handle transcript from VAD
   * Drops non-final transcripts when not armed (prevents "TV keeps talking" bug)
   * Always allows final transcripts through (OpenAI sends these after PTT release)
   */
  handleTranscript(transcript: string, isFinal: boolean = false): void {
    if (!transcript) return;

    // CRITICAL: Allow final transcripts through even when muted
    // OpenAI sends conversation.item.input_audio_transcription.completed AFTER
    // the user releases PTT, so we've already muted. Without this exception,
    // the user's final speech never appears in the UI or gets persisted.
    //
    // Only drop partial/delta transcripts when not armed to prevent ambient
    // noise accumulation ("TV keeps talking" bug).
    if (!this.armed && !this.handsFreeEnabled && !isFinal) {
      console.log('[VoiceController] Dropping partial transcript (not armed):', transcript.substring(0, 50));
      return;
    }

    // Emit final transcripts or any transcript when armed/hands-free
    console.log('[VoiceController] Emitting transcript (final=%s):', isFinal, transcript.substring(0, 50));
    eventBus.emit('voice_channel:transcript', { transcript, isFinal });
  }

  /**
   * Get VAD configuration for session setup
   */
  getVADConfig() {
    return {
      type: 'server_vad' as const,
      threshold: this.config.vadThreshold,
      silence_duration_ms: this.config.silenceDuration,
      prefix_padding_ms: this.config.prefixPadding
    };
  }

  /**
   * Release microphone resources
   */
  release(): void {
    console.log('[VoiceController] Releasing microphone');

    if (this.micStream) {
      this.micStream.getTracks().forEach(track => track.stop());
      this.micStream = null;
    }

    this.armed = false;
  }

  /**
   * Clean up resources
   */
  dispose(): void {
    this.release();
    this.session = null;
    console.log('[VoiceController] Disposed');
  }
}
