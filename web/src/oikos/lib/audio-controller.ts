/**
 * Audio Controller
 * Manages microphone streams and remote audio monitoring
 *
 * NOTE: Visualization has been removed. React components handle their own UI.
 */

import { logger } from '../core';

export enum AudioState {
  IDLE = 0,
  MIC_ACTIVE = 1 << 0,
  ASSISTANT_SPEAKING = 1 << 1,
}

export class AudioController {
  private micStream: MediaStream | null = null;

  // Speaker monitoring
  private audioState: AudioState = AudioState.IDLE;
  private speakerAudioCtx: AudioContext | null = null;
  private speakerAnalyser: AnalyserNode | null = null;
  private speakerDataArray: Uint8Array | null = null;
  private speakerSource: MediaStreamAudioSourceNode | MediaElementAudioSourceNode | null = null;
  private speakerStream: MediaStream | null = null;
  private speakerRafId: number | null = null;
  private speakerSilenceFrames = 0;
  private speakerMonitorUnavailable = false;

  // State
  private micLevel = 0;
  private speakerLevel = 0;

  // DOM references
  private remoteAudio: HTMLAudioElement | null = null;

  constructor() {}

  /**
   * Initialize with DOM elements
   */
  initialize(remoteAudio: HTMLAudioElement | null, _visualizationContainer?: HTMLElement | null): void {
    this.remoteAudio = remoteAudio;

    if (this.remoteAudio) {
      this.setupRemoteAudioListeners();
    }
  }

  /**
   * Acquire microphone access
   * Returns the raw stream - allows caller to mute/unmute tracks directly if needed
   */
  async requestMicrophone(): Promise<MediaStream> {
    if (this.micStream) {
      return this.micStream;
    }

    logger.info('🎙️ Requesting microphone access...');
    try {
      this.micStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      });
      logger.info('✅ Microphone access granted');
      return this.micStream;
    } catch (error) {
      logger.error('❌ Failed to request microphone:', error);
      throw error;
    }
  }

  /**
   * Clean up microphone stream
   */
  releaseMicrophone(): void {
    if (this.micStream) {
      logger.info('🧹 Releasing microphone stream');
      this.micStream.getTracks().forEach(track => {
        track.enabled = false;
        track.stop();
      });
      this.micStream = null;
    }

    this.setAudioStateFlag(AudioState.MIC_ACTIVE, false);
  }

  /**
   * Mute microphone (disable track)
   */
  muteMicrophone(): void {
    if (this.micStream) {
      const track = this.micStream.getAudioTracks()[0];
      if (track) track.enabled = false;
    }
  }

  /**
   * Unmute microphone (enable track)
   */
  unmuteMicrophone(): void {
    if (this.micStream) {
      const track = this.micStream.getAudioTracks()[0];
      if (track) track.enabled = true;
    }
  }

  /**
   * Update state for listening mode
   */
  async setListeningMode(active: boolean): Promise<void> {
    if (!active) {
      this.micLevel = 0;
    }

    this.setAudioStateFlag(AudioState.MIC_ACTIVE, active);

    // Update CSS class for any styling needs
    if (active) {
      document.body.classList.add('listening-mode');
    } else {
      document.body.classList.remove('listening-mode');
    }
  }

  /**
   * Start monitoring remote audio output
   */
  async startSpeakerMonitor(): Promise<void> {
    if (!this.remoteAudio || this.speakerMonitorUnavailable) return;

    try {
      if (!this.speakerAudioCtx) {
        this.speakerAudioCtx = new (window.AudioContext || (window as any).webkitAudioContext)();
      }

      if (this.speakerAudioCtx.state === 'suspended') {
        await this.speakerAudioCtx.resume().catch(() => undefined);
      }

      if (!this.speakerSource) {
        try {
          // Try captureStream first (modern browsers)
          if (typeof (this.remoteAudio as any).captureStream === 'function') {
            this.speakerStream = (this.remoteAudio as any).captureStream();
          }
        } catch (error) {
          logger.warn('Failed to capture remote audio stream', error);
          this.speakerStream = null;
        }

        if (this.speakerStream) {
          this.speakerSource = this.speakerAudioCtx.createMediaStreamSource(this.speakerStream);
        } else {
          // Fallback to ElementSource (CORS issues possible)
          this.speakerSource = this.speakerAudioCtx.createMediaElementSource(this.remoteAudio);
        }
      }

      if (!this.speakerAnalyser) {
        this.speakerAnalyser = this.speakerAudioCtx.createAnalyser();
        this.speakerAnalyser.fftSize = 1024;
        this.speakerAnalyser.smoothingTimeConstant = 0.7;
        this.speakerSource.connect(this.speakerAnalyser);
        this.speakerDataArray = new Uint8Array(this.speakerAnalyser.fftSize);
      }

      if (this.speakerRafId == null) {
        this.speakerSilenceFrames = 0;
        this.monitorSpeakerLevel();
      }
    } catch (error) {
      this.speakerMonitorUnavailable = true;
      logger.warn('Speaker visualizer unavailable', error);
    }
  }

  /**
   * Stop monitoring remote audio
   */
  stopSpeakerMonitor(): void {
    if (this.speakerRafId != null) {
      cancelAnimationFrame(this.speakerRafId);
      this.speakerRafId = null;
    }
    this.speakerSilenceFrames = 0;
    this.speakerLevel = 0;

    if (!this.hasAudioState(AudioState.MIC_ACTIVE)) {
      this.setAssistantSpeakingState(false);
    } else {
      this.updateAudioVisualization();
    }
  }

  /**
   * Dispose all resources
   */
  dispose(): void {
    this.releaseMicrophone();
    this.stopSpeakerMonitor();

    if (this.speakerAudioCtx) {
      this.speakerAudioCtx.close().catch(() => undefined);
      this.speakerAudioCtx = null;
    }

    this.speakerAnalyser = null;
    this.speakerDataArray = null;
    this.speakerSource = null;
    this.speakerStream = null;
  }

  // Internal Helpers

  private setupRemoteAudioListeners(): void {
    if (!this.remoteAudio) return;

    const handleStart = () => { void this.startSpeakerMonitor(); };
    const handleStop = () => { this.stopSpeakerMonitor(); };

    this.remoteAudio.addEventListener('play', handleStart);
    this.remoteAudio.addEventListener('playing', handleStart);
    this.remoteAudio.addEventListener('loadeddata', handleStart);
    this.remoteAudio.addEventListener('pause', handleStop);
    this.remoteAudio.addEventListener('ended', handleStop);
    this.remoteAudio.addEventListener('emptied', handleStop);
    this.remoteAudio.addEventListener('suspend', handleStop);
    this.remoteAudio.addEventListener('stalled', handleStop);
  }

  private monitorSpeakerLevel = (): void => {
    if (!this.speakerAnalyser || !this.speakerDataArray) {
      this.speakerRafId = null;
      return;
    }

    this.speakerAnalyser.getByteTimeDomainData(this.speakerDataArray as any);

    let sumSquares = 0;
    for (let i = 0; i < this.speakerDataArray.length; i++) {
      const centered = (this.speakerDataArray[i] - 128) / 128;
      sumSquares += centered * centered;
    }

    const rms = Math.sqrt(sumSquares / this.speakerDataArray.length);
    const level = Math.min(1, rms * 2.8);
    this.speakerLevel = level;
    this.updateAudioVisualization();

    if (level > 0.035) {
      this.speakerSilenceFrames = 0;
      if (!this.hasAudioState(AudioState.ASSISTANT_SPEAKING)) {
        this.setAssistantSpeakingState(true);
      }
    } else if (this.speakerSilenceFrames < 24) {
      this.speakerSilenceFrames += 1;
      if (this.speakerSilenceFrames === 24 && this.hasAudioState(AudioState.ASSISTANT_SPEAKING)) {
        this.setAssistantSpeakingState(false);
      }
    }

    this.speakerRafId = requestAnimationFrame(this.monitorSpeakerLevel);
  };

  private handleMicLevel(level: number): void {
    this.micLevel = level;
  }

  private updateAudioVisualization(): void {
    // Visualization removed - React components handle their own UI
    // This method is kept for internal state tracking
  }

  private setAssistantSpeakingState(active: boolean): void {
    this.setAudioStateFlag(AudioState.ASSISTANT_SPEAKING, active);
  }

  private hasAudioState(flag: AudioState): boolean {
    return (this.audioState & flag) === flag;
  }

  private setAudioStateFlag(flag: AudioState, enabled: boolean): void {
    const next = enabled ? (this.audioState | flag) : (this.audioState & ~flag);
    if (this.audioState !== next) {
      this.audioState = next as AudioState;
      this.syncAudioStateClasses();
      this.updateAudioVisualization();
    }
  }

  private syncAudioStateClasses(): void {
    const AUDIO_STATE_CLASSNAMES = ['audio-state-0', 'audio-state-1', 'audio-state-2', 'audio-state-3'];
    const classList = document.body.classList;
    for (const cls of AUDIO_STATE_CLASSNAMES) {
      classList.remove(cls);
    }
    classList.add(`audio-state-${this.audioState}`);
  }
}

export const audioController = new AudioController();
