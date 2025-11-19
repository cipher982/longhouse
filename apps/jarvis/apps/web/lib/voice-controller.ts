/**
 * Unified Voice Controller
 * Consolidates voiceManager + voiceChannelController into a single, clear domain owner
 * Handles ALL voice concerns: PTT, VAD, transcripts, gating, WebRTC, and UI state
 */

import { logger } from '@jarvis/core';
import type { RealtimeSession } from '@openai/agents/realtime';
import { eventBus } from './event-bus';

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
      
      // If we have an active microphone stream AND we are armed/hands-free,
      // attach it to the new session immediately.
      if (this.micStream && (this.state.armed || this.state.handsFree)) {
        logger.info('Attaching existing microphone stream to new session');
        try {
          // @ts-ignore - OpenAI types
          this.session.sendAudio(this.micStream);
        } catch (err) {
          logger.error('Failed to attach existing mic stream to new session:', err);
        }
      }
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

    // Stop microphone but keep session alive
    this.stopMicrophone();
  }

  // ============= VAD (Voice Activity Detection) =============

  /**
   * Handle VAD state changes from OpenAI
   */
  handleVADStateChange(active: boolean): void {
    console.log(`[DEBUG VoiceController] handleVADStateChange(${active}) - mode: ${this.state.mode}, handsFree: ${this.state.handsFree}`);

    if (this.state.mode !== 'vad' && !this.state.handsFree) {
      console.log('[DEBUG VoiceController] ❌ Ignoring VAD change - not in VAD mode or hands-free');
      return; // Ignore VAD when in PTT mode
    }

    console.log(`[DEBUG VoiceController] ✓ Processing VAD state change: ${active}`);

    this.setState({
      vadActive: active,
      active: active
    });

    // Call callback (new pattern)
    console.log('[DEBUG VoiceController] Calling onVADStateChange callback');
    this.config.onVADStateChange?.(active);

    if (active) {
      console.log('[DEBUG VoiceController] VAD active - starting microphone');
      this.startMicrophone().catch(err => {
        logger.error('Failed to start microphone for VAD:', err);
      });
    } else {
      console.log('[DEBUG VoiceController] VAD inactive');
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
    console.log(`[DEBUG VoiceController] setHandsFree(${enabled}) called`);

    // Guard: Can't enable hands-free in text mode
    if (enabled && this.isTextMode()) {
      console.warn('[VoiceController] Cannot enable hands-free in text mode. Transition to voice mode first.');
      return;
    }

    console.log(`[DEBUG VoiceController] ✓ Setting hands-free mode: ${enabled ? 'enabled' : 'disabled'}`);

    if (enabled) {
      // When enabling hands-free, arm the controller
      this.setState({
        handsFree: true,
        armed: true,
        mode: 'vad',
        pttActive: false
      });

      console.log('[DEBUG VoiceController] State set to hands-free, mode=vad, armed=true');
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
      console.log('[DEBUG VoiceController] State set to PTT mode, armed=false');
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
    try {
      // 1. Create stream if needed
      if (!this.micStream) {
        this.micStream = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true
          }
        });
        logger.info('Microphone started');
      }

      // 2. Send to OpenAI session if connected
      // (Always try to send/attach when starting microphone)
      if (this.session && this.micStream) {
        // @ts-ignore - OpenAI types may vary
        await this.session.sendAudio(this.micStream);
      }
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
   * Set up mouse/touch handlers for voice button (toggle mode)
   */
  setupButtonHandlers(button: HTMLElement): void {
    // Click-to-toggle behavior (not PTT hold-down)
    button.addEventListener('click', () => {
      // Don't interfere if hands-free mode is enabled
      if (this.state.handsFree) {
        return;
      }

      // Toggle: if active, stop; if inactive, start
      if (this.state.pttActive || this.state.armed) {
        this.stopPTT();
      } else {
        this.startPTT();
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
   * Convert state to InteractionState for event bus (compatibility)
   */
  private toInteractionState(state: VoiceState) {
    if (state.interactionMode === 'voice') {
      return {
        mode: 'voice' as const,
        armed: state.armed,
        handsFree: state.handsFree
      };
    } else {
      return {
        mode: 'text' as const
      };
    }
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

    // Emit state:changed event for backward compatibility with tests
    eventBus.emit('state:changed', {
      from: this.toInteractionState(from),
      to: this.toInteractionState(this.state),
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

    // Emit state:changed event for backward compatibility with tests
    eventBus.emit('state:changed', {
      from: this.toInteractionState(from),
      to: this.toInteractionState(this.state),
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

// ============= Compatibility Methods (kept in base class) =============

// Compatibility interface extension
declare module './voice-controller' {
  interface VoiceController {
    // Properties
    armed: boolean;
    handsFreeEnabled: boolean;

    // Methods
    isArmed(): boolean;
    isHandsFreeEnabled(): boolean;
    arm(): void;
    mute(): void;
    armVoice(): void;
    muteVoice(): void;
    isVoiceArmed(): boolean;
    getInteractionState(): { mode: string; armed: boolean; handsFree: boolean };
    handleSpeechStart(): void;
    handleSpeechStop(): void;
    initialize(): Promise<void>;
    requestMicrophone(): Promise<MediaStream>;
    getMicrophoneStream(): MediaStream | null;
    release(): void;
  }
}

// Compatibility methods for old interface - kept in VoiceController for now
// These will be removed in Phase 6

// Compatibility properties
Object.defineProperty(VoiceController.prototype, 'armed', {
  get(this: VoiceController) {
    return this.getState().armed;
  }
});

Object.defineProperty(VoiceController.prototype, 'handsFreeEnabled', {
  get(this: VoiceController) {
    return this.getState().handsFree;
  }
});

// Add compatibility methods to VoiceController prototype
Object.assign(VoiceController.prototype, {
  isArmed(this: VoiceController): boolean {
    return this.getState().armed;
  },

  isHandsFreeEnabled(this: VoiceController): boolean {
    return this.getState().handsFree;
  },

  arm(this: VoiceController): void {
    const from = this.getState();
    this.startPTT();
    // Emit events for backward compatibility with tests
    eventBus.emit('voice_channel:armed', { armed: true });
    eventBus.emit('state:changed', {
      // @ts-ignore - accessing private method
      from: this.toInteractionState(from),
      // @ts-ignore - accessing private method
      to: this.toInteractionState(this.getState()),
      timestamp: Date.now()
    });
  },

  mute(this: VoiceController): void {
    const from = this.getState();
    this.stopPTT();
    // Emit events for backward compatibility with tests
    eventBus.emit('voice_channel:muted', { muted: true });
    eventBus.emit('state:changed', {
      // @ts-ignore - accessing private method
      from: this.toInteractionState(from),
      // @ts-ignore - accessing private method
      to: this.toInteractionState(this.getState()),
      timestamp: Date.now()
    });
  },

  armVoice(this: VoiceController): void {
    const from = this.getState();
    this.startPTT();
    // Emit events for backward compatibility with tests
    eventBus.emit('voice_channel:armed', { armed: true });
    eventBus.emit('state:changed', {
      // @ts-ignore - accessing private method
      from: this.toInteractionState(from),
      // @ts-ignore - accessing private method
      to: this.toInteractionState(this.getState()),
      timestamp: Date.now()
    });
  },

  muteVoice(this: VoiceController): void {
    const from = this.getState();
    this.stopPTT();
    // Emit events for backward compatibility with tests
    eventBus.emit('voice_channel:muted', { muted: true });
    eventBus.emit('state:changed', {
      // @ts-ignore - accessing private method
      from: this.toInteractionState(from),
      // @ts-ignore - accessing private method
      to: this.toInteractionState(this.getState()),
      timestamp: Date.now()
    });
  },

  isVoiceArmed(this: VoiceController): boolean {
    return this.isVoiceMode() && this.getState().armed;
  },

  getInteractionState(this: VoiceController) {
    const state = this.getState();
    return {
      mode: state.interactionMode,
      armed: state.armed,
      handsFree: state.handsFree
    };
  },

  handleSpeechStart(this: VoiceController): void {
    console.log('[DEBUG VoiceController] ✓ handleSpeechStart() called');
    this.handleVADStateChange(true);
  },

  handleSpeechStop(this: VoiceController): void {
    console.log('[DEBUG VoiceController] ✓ handleSpeechStop() called');
    this.handleVADStateChange(false);
  },

  async initialize(this: VoiceController): Promise<void> {
    // No-op - initialization happens in constructor
  },

  async requestMicrophone(this: VoiceController): Promise<MediaStream> {
    // @ts-ignore - accessing protected property
    if (this.micStream) {
      // @ts-ignore
      return this.micStream;
    }

    try {
      // @ts-ignore
      this.micStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      });
      logger.info('Microphone requested for initialization');
      // @ts-ignore
      return this.micStream;
    } catch (error) {
      logger.error('Failed to request microphone:', error);
      throw error;
    }
  },

  getMicrophoneStream(this: VoiceController): MediaStream | null {
    // @ts-ignore - accessing protected property
    return this.micStream;
  },

  release(this: VoiceController): void {
    this.dispose();
  }
});

// Export singleton instance (will be configured in main.ts)
export let voiceController: VoiceController;