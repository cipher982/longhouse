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
  interactionMode: 'voice' | 'text';  // Overall interaction mode
  active: boolean;  // Is mic currently hot?
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
}

// Events emitted by VoiceController
export type VoiceEvent =
  | { type: 'stateChange', state: VoiceState }
  | { type: 'transcript', text: string, isFinal: boolean }
  | { type: 'error', error: Error }
  | { type: 'vadStateChange', active: boolean };

type VoiceListener = (event: VoiceEvent) => void;

export class VoiceController {
  private state: VoiceState = {
    mode: 'ptt',
    interactionMode: 'voice',
    active: false,
    handsFree: false,
    transcript: '',
    finalTranscript: '',
    vadActive: false,
    pttActive: false
  };

  private session: RealtimeSession | null = null;
  protected micStream: MediaStream | null = null;
  private config: Required<VoiceConfig>;
  private listeners: Set<VoiceListener> = new Set();

  constructor(config: VoiceConfig = {}) {
    this.config = {
      vadThreshold: 0.5,
      silenceDuration: 500,
      prefixPadding: 300,
      ...config
    };
  }

  // ============= Event System =============

  addListener(listener: VoiceListener): void {
    this.listeners.add(listener);
    // Immediate callback with current state
    listener({ type: 'stateChange', state: this.getState() });
  }

  removeListener(listener: VoiceListener): void {
    this.listeners.delete(listener);
  }

  private emit(event: VoiceEvent): void {
    this.listeners.forEach(l => l(event));
  }

  // ============= Core State Management =============

  getState(): Readonly<VoiceState> {
    return { ...this.state };
  }

  private setState(updates: Partial<VoiceState>): void {
    const oldState = { ...this.state };
    this.state = { ...this.state, ...updates };

    if (oldState.active !== this.state.active) {
      logger.info(`Voice ${this.state.active ? 'activated' : 'deactivated'}`);
    }

    this.emit({ type: 'stateChange', state: this.getState() });
  }

  // ============= Session Management =============

  isConnected(): boolean {
    return !!this.session;
  }

  setSession(session: RealtimeSession | null): void {
    this.session = session;

    if (session) {
      logger.info('Voice session connected');
    } else {
      logger.info('Voice session disconnected');
      this.setState({ active: false, pttActive: false });
    }
  }

  setMicrophoneStream(stream: MediaStream): void {
    this.micStream = stream;
    logger.info('Microphone stream attached to voice controller');
    this.muteAudio();
  }

  // ============= PTT (Push-to-Talk) =============

  startPTT(): void {
    logger.info('PTT pressed - activating voice');

    this.setState({
      mode: 'ptt',
      interactionMode: 'voice',
      active: true,
      pttActive: true,
      transcript: '',
      finalTranscript: ''
    });

    if (this.session) {
      this.unmuteAudio();
    } else {
      logger.warn('No session available, skipping microphone start');
    }
  }

  stopPTT(): void {
    logger.info('PTT released - muting voice');

    this.setState({
      active: false,
      pttActive: false
    });

    this.muteAudio();
  }

  // ============= VAD (Voice Activity Detection) =============

  handleVADStateChange(active: boolean): void {
    if (this.state.mode !== 'vad' && !this.state.handsFree) {
      return;
    }

    this.setState({
      vadActive: active,
      active: active
    });

    this.emit({ type: 'vadStateChange', active });

    if (active) {
      this.unmuteAudio();
    } else if (!this.state.handsFree) {
      this.muteAudio();
    }
  }

  setHandsFree(enabled: boolean): void {
    if (enabled && this.isTextMode()) {
      console.warn('[VoiceController] Cannot enable hands-free in text mode.');
      return;
    }

    if (enabled) {
      this.setState({
        interactionMode: 'voice',
        handsFree: true,
        mode: 'vad',
        pttActive: false
      });
      this.unmuteAudio();
    } else {
      this.setState({
        handsFree: false,
        active: false,
        mode: 'ptt'
      });
      this.muteAudio();
    }
  }

  // ============= Transcript Handling =============

  handleTranscript(text: string, isFinal: boolean): void {
    if (!text) return;

    if (!isFinal) {
      this.setState({ transcript: text });
    } else {
      this.setState({
        transcript: '',
        finalTranscript: text
      });
    }

    this.emit({ type: 'transcript', text, isFinal });
  }

  // ============= Microphone Management =============

  private unmuteAudio(): void {
    if (!this.micStream) {
      logger.warn('Cannot unmute: no mic stream available');
      return;
    }
    const audioTrack = this.micStream.getAudioTracks()[0];
    if (audioTrack) {
      audioTrack.enabled = true;
    }
  }

  private muteAudio(): void {
    if (!this.micStream) return;
    const audioTrack = this.micStream.getAudioTracks()[0];
    if (audioTrack) {
      audioTrack.enabled = false;
    }
  }

  // ============= Keyboard Support =============

  setupKeyboardHandlers(element: HTMLElement): void {
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

  // ============= Interaction Mode Management =============

  isVoiceMode(): boolean {
    return this.state.interactionMode === 'voice';
  }

  isTextMode(): boolean {
    return this.state.interactionMode === 'text';
  }

  transitionToVoice(options?: { handsFree?: boolean }): void {
    this.setState({
      interactionMode: 'voice',
      handsFree: options?.handsFree ?? false
    });
    logger.info('Transitioned to voice mode', options);
  }

  transitionToText(): void {
    if (this.state.pttActive) {
      this.stopPTT();
    }
    this.setState({ interactionMode: 'text', active: false });
    logger.info('Transitioned to text mode');
  }

  // ============= VAD Configuration =============

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
   * Reset state and session (keeps listeners attached)
   */
  reset(): void {
    this.muteAudio();
    this.session = null;
    this.setState({
      mode: 'ptt',
      interactionMode: 'voice',
      active: false,
      handsFree: false,
      pttActive: false,
      vadActive: false,
      transcript: '',
      finalTranscript: ''
    });
    logger.info('Voice controller reset');
  }

  dispose(): void {
    this.reset();
    this.listeners.clear();
    logger.info('Voice controller disposed');
  }

  // ============= Compatibility Methods (Deprecated) =============
  // Kept for existing tests that might call these directly
  // These will be removed in future cleanups

  handleSpeechStart(): void {
    this.handleVADStateChange(true);
  }

  handleSpeechStop(): void {
    this.handleVADStateChange(false);
  }

  async initialize(): Promise<void> {}
}

export let voiceController: VoiceController;

export function initializeVoiceController(config: VoiceConfig): VoiceController {
  voiceController = new VoiceController(config);
  return voiceController;
}
