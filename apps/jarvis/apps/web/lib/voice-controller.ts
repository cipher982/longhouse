/**
 * Unified Voice Controller
 * Consolidates voiceManager + voiceChannelController into a single, clear domain owner
 * Handles ALL voice concerns: PTT, VAD, transcripts, gating, WebRTC, and UI state
 */

import { logger } from '@jarvis/core';
import type { RealtimeSession } from '@openai/agents/realtime';

// Voice state - single source of truth
export interface VoiceState {
  mode: 'ptt' | 'vad' | 'off';
  active: boolean;  // Is mic currently hot?
  armed: boolean;   // Ready to receive input?
  handsFree: boolean;
  transcript: string;
  finalTranscript: string;
  vadActive: boolean;
  pttActive: boolean;
}

// Configuration
export interface VoiceConfig {
  vadThreshold?: number;
  silenceDuration?: number;
  prefixPadding?: number;
  onStateChange?: (state: VoiceState) => void;
  onFinalTranscript?: (text: string) => void;
  onError?: (error: Error) => void;
}

export class VoiceController {
  private state: VoiceState = {
    mode: 'ptt',
    active: false,
    armed: false,
    handsFree: false,
    transcript: '',
    finalTranscript: '',
    vadActive: false,
    pttActive: false
  };

  private session: RealtimeSession | null = null;
  protected micStream: MediaStream | null = null;  // Protected for compatibility layer
  private audioContext: AudioContext | null = null;
  private config: VoiceConfig;

  constructor(config: VoiceConfig = {}) {
    this.config = {
      vadThreshold: 0.5,
      silenceDuration: 500,
      prefixPadding: 300,
      ...config
    };
  }

  // ============= Core State Management =============

  /**
   * Get current voice state (read-only)
   */
  getState(): Readonly<VoiceState> {
    return { ...this.state };
  }

  /**
   * Update state and notify listeners
   */
  private setState(updates: Partial<VoiceState>): void {
    const oldState = { ...this.state };
    this.state = { ...this.state, ...updates };

    // Log significant state changes
    if (oldState.active !== this.state.active) {
      logger.info(`Voice ${this.state.active ? 'activated' : 'deactivated'}`);
    }

    // Notify listener
    this.config.onStateChange?.(this.getState());
  }

  // ============= Session Management =============

  /**
   * Set the OpenAI realtime session
   */
  setSession(session: RealtimeSession | null): void {
    this.session = session;

    if (session) {
      logger.info('Voice session connected');
      // Session will send transcript events to our handleTranscript method
    } else {
      logger.info('Voice session disconnected');
      this.setState({ active: false, armed: false });
    }
  }

  // ============= PTT (Push-to-Talk) =============

  /**
   * Start push-to-talk
   */
  startPTT(): void {
    if (!this.session) {
      this.config.onError?.(new Error('No active session'));
      return;
    }

    logger.info('PTT pressed - arming voice');

    this.setState({
      mode: 'ptt',
      active: true,
      armed: true,
      pttActive: true,
      transcript: '',
      finalTranscript: ''
    });

    // Start microphone capture
    this.startMicrophone().catch(err => {
      logger.error('Failed to start microphone:', err);
      this.config.onError?.(err);
    });
  }

  /**
   * Stop push-to-talk
   */
  stopPTT(): void {
    logger.info('PTT released - muting voice');

    this.setState({
      active: false,
      armed: false,
      pttActive: false
    });

    // Stop microphone but keep session alive
    this.stopMicrophone();
  }

  // ============= VAD (Voice Activity Detection) =============

  /**
   * Handle VAD state changes from OpenAI
   */
  handleVADStateChange(active: boolean): void {
    if (this.state.mode !== 'vad' && !this.state.handsFree) {
      return; // Ignore VAD when in PTT mode
    }

    logger.debug('VAD state changed:', active);

    this.setState({
      vadActive: active,
      active: active
    });

    if (active) {
      this.startMicrophone().catch(err => {
        logger.error('Failed to start microphone for VAD:', err);
      });
    } else {
      // Keep mic open in hands-free, just not "active"
      if (!this.state.handsFree) {
        this.stopMicrophone();
      }
    }
  }

  /**
   * Enable/disable hands-free mode
   */
  setHandsFree(enabled: boolean): void {
    logger.info(`Hands-free mode ${enabled ? 'enabled' : 'disabled'}`);

    this.setState({
      handsFree: enabled,
      armed: enabled,
      mode: enabled ? 'vad' : 'ptt',
      pttActive: false  // Stop PTT when switching to hands-free
    });

    if (enabled) {
      this.startMicrophone().catch(err => {
        logger.error('Failed to start microphone for hands-free:', err);
      });
    } else {
      this.stopMicrophone();
    }
  }

  // ============= Transcript Handling (with Gating) =============

  /**
   * Handle incoming transcript from WebSocket
   * This is the SINGLE entry point for all transcripts
   */
  handleTranscript(text: string, isFinal: boolean): void {
    if (!text) return;

    // CRITICAL GATING LOGIC
    // Allow final transcripts through even when muted (OpenAI sends finals after PTT release)
    // Block partial transcripts when not armed to prevent ambient noise
    if (!this.state.armed && !this.state.handsFree && !isFinal) {
      logger.debug('Dropping partial transcript (not armed):', text.substring(0, 50));
      return;
    }

    // Update state
    if (!isFinal) {
      this.setState({ transcript: text });
    } else {
      this.setState({
        transcript: '',
        finalTranscript: text
      });

      // Notify listener of final transcript
      this.config.onFinalTranscript?.(text);
    }

    logger.debug(`Transcript (final=${isFinal}):`, text.substring(0, 50));
  }

  // ============= Microphone Management =============

  /**
   * Start microphone capture
   */
  private async startMicrophone(): Promise<void> {
    if (this.micStream) return; // Already started

    try {
      this.micStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      });

      // Send to OpenAI session if connected
      if (this.session && this.micStream) {
        // @ts-ignore - OpenAI types may vary
        await this.session.sendAudio(this.micStream);
      }

      logger.info('Microphone started');
    } catch (error) {
      logger.error('Failed to access microphone:', error);
      throw error;
    }
  }

  /**
   * Stop microphone capture
   */
  private stopMicrophone(): void {
    if (this.micStream) {
      this.micStream.getTracks().forEach(track => track.stop());
      this.micStream = null;
      logger.info('Microphone stopped');
    }
  }

  // ============= Keyboard Support =============

  /**
   * Set up keyboard event handlers on an element
   */
  setupKeyboardHandlers(element: HTMLElement): void {
    // Space/Enter for PTT
    element.addEventListener('keydown', (e) => {
      if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) {
        e.preventDefault();
        if (!this.state.pttActive) {
          this.startPTT();
        }
      }
    });

    element.addEventListener('keyup', (e) => {
      if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        if (this.state.pttActive) {
          this.stopPTT();
        }
      }
    });
  }

  /**
   * Set up mouse/touch handlers for PTT button
   */
  setupButtonHandlers(button: HTMLElement): void {
    // Mouse events
    button.addEventListener('mousedown', () => {
      if (!this.state.handsFree) {
        this.startPTT();
      }
    });

    button.addEventListener('mouseup', () => {
      if (this.state.pttActive) {
        this.stopPTT();
      }
    });

    button.addEventListener('mouseleave', () => {
      if (this.state.pttActive) {
        this.stopPTT();
      }
    });

    // Touch events
    button.addEventListener('touchstart', (e) => {
      e.preventDefault();
      if (!this.state.handsFree) {
        this.startPTT();
      }
    });

    button.addEventListener('touchend', (e) => {
      e.preventDefault();
      if (this.state.pttActive) {
        this.stopPTT();
      }
    });
  }

  // ============= VAD Configuration =============

  /**
   * Get VAD config for OpenAI session
   */
  getVADConfig() {
    return {
      type: 'server_vad' as const,
      threshold: this.config.vadThreshold,
      silence_duration_ms: this.config.silenceDuration,
      prefix_padding_ms: this.config.prefixPadding
    };
  }

  // ============= Cleanup =============

  /**
   * Clean up resources
   */
  dispose(): void {
    this.stopMicrophone();
    this.session = null;
    this.setState({
      active: false,
      armed: false,
      pttActive: false,
      vadActive: false,
      transcript: '',
      finalTranscript: ''
    });
    logger.info('Voice controller disposed');
  }
}

// ============= Compatibility Methods =============
// These methods help with migration from old voice modules

export class VoiceControllerCompat extends VoiceController {
  // Compatibility properties for old interface
  get armed(): boolean {
    return this.getState().armed;
  }

  get handsFreeEnabled(): boolean {
    return this.getState().handsFree;
  }

  /**
   * Compatibility: Check if armed (for gating logic)
   */
  isArmed(): boolean {
    return this.getState().armed;
  }

  /**
   * Compatibility: Check if hands-free is enabled
   */
  isHandsFreeEnabled(): boolean {
    return this.getState().handsFree;
  }

  /**
   * Compatibility: Arm the voice channel (start PTT)
   */
  arm(): void {
    this.startPTT();
  }

  /**
   * Compatibility: Mute the voice channel (stop PTT)
   */
  mute(): void {
    this.stopPTT();
  }

  /**
   * Compatibility: Handle speech start (VAD)
   */
  handleSpeechStart(): void {
    this.handleVADStateChange(true);
  }

  /**
   * Compatibility: Handle speech stop (VAD)
   */
  handleSpeechStop(): void {
    this.handleVADStateChange(false);
  }

  /**
   * Compatibility: Initialize (no-op for now)
   */
  async initialize(): Promise<void> {
    // No-op - initialization happens in constructor
  }

  /**
   * Compatibility: Request microphone
   */
  async requestMicrophone(): Promise<MediaStream> {
    // Start mic and return stream
    await this.startPTT();
    // For now, return a mock stream - real implementation would track the stream
    return new MediaStream();
  }

  /**
   * Compatibility: Get microphone stream
   */
  getMicrophoneStream(): MediaStream | null {
    return this.micStream;
  }

  /**
   * Compatibility: Release resources
   */
  release(): void {
    this.dispose();
  }
}

// Export singleton instance (will be configured in main.ts)
export let voiceController: VoiceControllerCompat;