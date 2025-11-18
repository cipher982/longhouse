/**
 * Unified Voice Controller
 * Consolidates voiceManager + voiceChannelController into a single, clear domain owner
 * Handles ALL voice concerns: PTT, VAD, transcripts, gating, WebRTC, and UI state
 */

import { logger } from '@jarvis/core';
import type { RealtimeSession } from '@openai/agents/realtime';
import { eventBus, type InteractionState } from './event-bus';

// Voice state - single source of truth
export interface VoiceState {
  mode: 'ptt' | 'vad' | 'off';
  interactionMode: 'voice' | 'text';  // Overall interaction mode
  active: boolean;  // Is mic currently hot?
  armed: boolean;   // Ready to receive input?
  handsFree: boolean;
  transcript: string;
  finalTranscript: string;
  vadActive: boolean;
  pttActive: boolean;
}

// Configuration with comprehensive callback API
export interface VoiceConfig {
  // VAD Configuration
  vadThreshold?: number;
  silenceDuration?: number;
  prefixPadding?: number;

  // Lifecycle Callbacks (replace event-bus)
  onStateChange?: (state: VoiceState) => void;
  onArmed?: () => void;
  onMuted?: () => void;
  onTranscript?: (text: string, isFinal: boolean) => void;
  onFinalTranscript?: (text: string) => void;
  onVADStateChange?: (active: boolean) => void;
  onModeTransition?: (from: 'voice' | 'text', to: 'voice' | 'text') => void;
  onError?: (error: Error) => void;
}

export class VoiceController {
  private state: VoiceState = {
    mode: 'ptt',
    interactionMode: 'voice',
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
   * Convert VoiceState to InteractionState for event bus
   */
  private toInteractionState(state?: VoiceState): InteractionState {
    const s = state || this.state;
    if (s.interactionMode === 'voice') {
      return {
        mode: 'voice',
        armed: s.armed,
        handsFree: s.handsFree
      };
    } else {
      return {
        mode: 'text'
      };
    }
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
    logger.info('PTT pressed - arming voice');

    const from = { ...this.state };

    this.setState({
      mode: 'ptt',
      active: true,
      armed: true,
      pttActive: true,
      transcript: '',
      finalTranscript: ''
    });

    // Call callbacks (new pattern)
    this.config.onArmed?.();

    // Emit events for backward compatibility (temporary)
    eventBus.emit('voice_channel:armed', { armed: true });
    eventBus.emit('state:changed', {
      from: this.toInteractionState(from),
      to: this.toInteractionState(),
      timestamp: Date.now()
    });

    // Start microphone capture if session exists
    if (this.session) {
      this.startMicrophone().catch(err => {
        logger.error('Failed to start microphone:', err);
        this.config.onError?.(err);
      });
    } else {
      logger.warn('No session available, skipping microphone start');
    }
  }

  /**
   * Stop push-to-talk
   * @param skipEvents - Skip event emissions (used internally for mode transitions)
   */
  stopPTT(skipEvents: boolean = false): void {
    logger.info('PTT released - muting voice');

    const from = { ...this.state };

    this.setState({
      active: false,
      armed: false,
      pttActive: false
    });

    // Call callbacks (new pattern)
    if (!skipEvents) {
      this.config.onMuted?.();
    }

    // Emit events for backward compatibility (unless called from mode transition)
    if (!skipEvents) {
      eventBus.emit('voice_channel:muted', { muted: true });
      eventBus.emit('state:changed', {
        from: this.toInteractionState(from),
        to: this.toInteractionState(),
        timestamp: Date.now()
      });
    }

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

    // Call callback (new pattern)
    this.config.onVADStateChange?.(active);

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
    // Guard: Can't enable hands-free in text mode
    if (enabled && this.isTextMode()) {
      console.warn('[VoiceController] Cannot enable hands-free in text mode. Transition to voice mode first.');
      return;
    }

    logger.info(`Hands-free mode ${enabled ? 'enabled' : 'disabled'}`);

    if (enabled) {
      // When enabling hands-free, arm the controller
      this.setState({
        handsFree: true,
        armed: true,
        mode: 'vad',
        pttActive: false
      });

      this.startMicrophone().catch(err => {
        logger.error('Failed to start microphone for hands-free:', err);
      });
    } else {
      // When disabling hands-free, unarm back to PTT-ready state
      this.setState({
        handsFree: false,
        armed: false,
        active: false,
        mode: 'ptt'
      });
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

    // Call transcript callback (new pattern)
    this.config.onTranscript?.(text, isFinal);

    // Emit event for backward compatibility with tests
    eventBus.emit('voice_channel:transcript', { transcript: text, isFinal });

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

  // ============= Interaction Mode Management =============

  /**
   * Check if in voice interaction mode
   */
  isVoiceMode(): boolean {
    return this.state.interactionMode === 'voice';
  }

  /**
   * Check if in text interaction mode
   */
  isTextMode(): boolean {
    return this.state.interactionMode === 'text';
  }

  /**
   * Transition to voice interaction mode
   */
  transitionToVoice(options?: { armed?: boolean; handsFree?: boolean }): void {
    const from = { ...this.state };
    const fromMode = from.interactionMode;

    this.setState({
      interactionMode: 'voice',
      armed: options?.armed ?? false,
      handsFree: options?.handsFree ?? false
    });

    // Call callbacks (new pattern)
    if (fromMode !== 'voice') {
      this.config.onModeTransition?.(fromMode, 'voice');
    }
    if (options?.armed) {
      this.config.onArmed?.();
    }

    // If arming, emit the armed event (backward compatibility)
    if (options?.armed) {
      eventBus.emit('voice_channel:armed', { armed: true });
    }

    // Emit state:changed event for backward compatibility
    eventBus.emit('state:changed', {
      from: this.toInteractionState(from),
      to: this.toInteractionState(),
      timestamp: Date.now()
    });

    logger.info('Transitioned to voice mode', options);
  }

  /**
   * Transition to text interaction mode
   */
  transitionToText(): void {
    const from = { ...this.state };
    const fromMode = from.interactionMode;

    // Mute voice when switching to text (skip events to avoid double emission)
    if (this.state.armed) {
      this.stopPTT(true);  // Skip events - we'll call callbacks below
    }

    this.setState({
      interactionMode: 'text'
    });

    // Call callback (new pattern)
    if (fromMode !== 'text') {
      this.config.onModeTransition?.(fromMode, 'text');
    }

    // Emit single state:changed event for the mode transition (backward compatibility)
    eventBus.emit('state:changed', {
      from: this.toInteractionState(from),
      to: this.toInteractionState(),
      timestamp: Date.now()
    });

    logger.info('Transitioned to text mode');
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
      interactionMode: 'voice',
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
  // Note: No longer subscribes to state:changed events
  // VoiceController is now the source of truth for interaction state

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
    // Event is now emitted in startPTT()
  }

  /**
   * Compatibility: Mute the voice channel (stop PTT)
   */
  mute(): void {
    this.stopPTT();
    // Event is now emitted in stopPTT()
  }

  /**
   * Compatibility: Arm voice (alias for arm)
   */
  armVoice(): void {
    this.arm();
  }

  /**
   * Compatibility: Mute voice (alias for mute)
   */
  muteVoice(): void {
    this.mute();
  }

  /**
   * Compatibility: Check if voice is armed
   */
  isVoiceArmed(): boolean {
    return this.isVoiceMode() && this.isArmed();
  }

  /**
   * Compatibility: Get state (for backward compatibility with stateMachine.getState())
   */
  getInteractionState() {
    const state = this.getState();
    return {
      mode: state.interactionMode,
      armed: state.armed,
      handsFree: state.handsFree
    };
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
    // Just get microphone stream without requiring session
    if (this.micStream) {
      return this.micStream;
    }

    try {
      this.micStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      });
      logger.info('Microphone requested for initialization');
      return this.micStream;
    } catch (error) {
      logger.error('Failed to request microphone:', error);
      throw error;
    }
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