/**
 * App Controller
 * High-level orchestrator for the Jarvis Application.
 * Coordinates Audio, Voice, Session, and UI State.
 */

import { type RealtimeSession } from '@openai/agents/realtime';
import { logger } from '@jarvis/core';
import { stateManager } from './state-manager';
import { sessionHandler } from './session-handler';
import { audioController } from './audio-controller';
import { voiceController, type VoiceEvent } from './voice-controller';
import { conversationController } from './conversation-controller';
import { uiEnhancements } from './ui-enhancements';
import { uiController } from './ui-controller'; // Import UI Controller directly
import { feedbackSystem } from './feedback-system';
import { VoiceButtonState, CONFIG } from './config';
import { TextChannelController } from './text-channel-controller';
import { createContextTools } from './tool-factory';

export class AppController {
  private initialized = false;
  private connecting = false;
  private textChannelController: TextChannelController | null = null;

  constructor() {
    // Bind methods to this
    this.connect = this.connect.bind(this);
    this.disconnect = this.disconnect.bind(this);
  }

  /**
   * Initialize the application controllers and logic
   */
  async initialize(): Promise<void> {
    if (this.initialized) return;

    logger.info('🚀 Initializing App Controller...');

    // 1. Setup Event Listeners
    this.setupVoiceListeners();

    // 2. Initialize Text Channel Controller
    this.textChannelController = new TextChannelController({
      autoConnect: true,
      maxRetries: 3
    });
    this.textChannelController.setVoiceController(voiceController);
    this.textChannelController.setConnectCallback(this.connect);

    // 3. Async initialization
    await this.textChannelController.initialize();
    
    // Initialize UI
    uiController.initialize();

    this.initialized = true;
    logger.info('✅ App Controller initialized');
  }

  /**
   * Connect to Voice Session
   */
  async connect(): Promise<void> {
    if (this.connecting) return;
    this.connecting = true;

    logger.info('🔗 Connect sequence starting...');
    
    let loadingOverlay: HTMLDivElement | null = null;
    
    try {
      uiController.updateButtonState(VoiceButtonState.CONNECTING);
      loadingOverlay = uiEnhancements.showLoading('Requesting microphone access...');

      const currentContext = stateManager.getState().currentContext;
      
      // 1. Acquire Microphone (via AudioController)
      const micStream = await audioController.requestMicrophone();
      
      // PRIVACY-CRITICAL: Mute immediately
      audioController.muteMicrophone();

      if (loadingOverlay) {
        loadingOverlay.querySelector('.loading-text')!.textContent = 'Connecting to OpenAI...';
      }

      // 2. Connect Session
      if (!currentContext) {
        throw new Error('No active context loaded');
      }
      
      // Create tools using factory
      const tools = createContextTools(currentContext, stateManager.getState().sessionManager);

      const { session, agent } = await sessionHandler.connect({
        context: currentContext,
        mediaStream: micStream,
        audioElement: undefined, 
        tools: tools,
        onTokenRequest: this.getSessionToken
      });

      // 3. Update State
      stateManager.setSession(session);
      stateManager.setAgent(agent);
      
      // 4. Wire up Session Events
      this.setupSessionEvents(session);

      // 5. Wire up Controllers
      voiceController.setSession(session);
      voiceController.setMicrophoneStream(micStream);
      this.textChannelController?.setSession(session);

      // 6. Finalize UI State
      uiController.updateButtonState(VoiceButtonState.READY);
      this.connecting = false;
      
      // Default to voice mode
      if (voiceController.isTextMode()) {
        voiceController.transitionToVoice({ armed: false, handsFree: false });
      }

      // Feedback
      feedbackSystem.playConnectChime();
      if (loadingOverlay) uiEnhancements.hideLoading(loadingOverlay);
      uiEnhancements.showToast('Connected successfully', 'success');

    } catch (error: any) {
      logger.error('Connection failed', error);
      this.connecting = false;
      
      // Cleanup
      audioController.releaseMicrophone();
      uiController.updateButtonState(VoiceButtonState.IDLE);
      
      if (loadingOverlay) uiEnhancements.hideLoading(loadingOverlay);
      uiEnhancements.showToast(`Connection failed: ${error.message}`, 'error');
      feedbackSystem.playErrorTone();
    }
  }

  /**
   * Disconnect from Voice Session
   */
  async disconnect(): Promise<void> {
    logger.info('🔌 Disconnect sequence starting...');
    
    // UI Feedback
    uiEnhancements.showToast('Disconnecting...', 'info');
    audioController.setListeningMode(false);

    try {
      // 1. Disconnect Session
      await sessionHandler.disconnect();
      
      // 2. Cleanup State
      stateManager.setSession(null);
      voiceController.setSession(null);
      voiceController.reset(); // Clears mic, flags, but keeps listeners
      this.textChannelController?.setSession(null);
      
      // 3. Cleanup Audio
      audioController.dispose(); // Releases mic and stops monitor

      logger.info('✅ Disconnected successfully');
      uiEnhancements.showToast('Disconnected', 'info');

    } catch (error) {
      logger.error('Disconnect error', error);
    } finally {
      uiController.updateButtonState(VoiceButtonState.IDLE);
    }
  }

  // ================= PRIVATE HELPERS =================

  private setupVoiceListeners() {
    voiceController.addListener((event: VoiceEvent) => {
      switch (event.type) {
        case 'stateChange':
          this.handleVoiceStateChange(event.state);
          break;
        case 'transcript':
          if (!event.isFinal) {
            conversationController.updateUserPreview(event.text);
          } else {
            this.handleUserTranscript(event.text);
          }
          break;
        case 'vadStateChange':
          if (event.active) {
            feedbackSystem.playVoiceTick();
          }
          break;
        case 'error':
          logger.error('Voice controller error:', event.error);
          uiEnhancements.showToast(`Voice error: ${event.error.message}`, 'error');
          break;
      }
    });
  }

  private handleVoiceStateChange(state: any) {
    // Handle audio feedback via AudioController
    // VAD active means we should visualize listening
    if (state.vadActive || state.active) {
      audioController.setListeningMode(true).catch(() => {});
      uiController.updateButtonState(VoiceButtonState.SPEAKING); // Or SPEAKING/ACTIVE
    } else {
      audioController.setListeningMode(false).catch(() => {});
      // If connected but not active, set to READY
      if (voiceController.isConnected()) {
         uiController.updateButtonState(VoiceButtonState.READY);
      }
    }
    
    // Handle mic mute state
    if (state.active) {
      audioController.unmuteMicrophone();
    } else {
      audioController.muteMicrophone();
    }
  }

  private async handleUserTranscript(text: string): Promise<void> {
    const finalText = text.trim();
    if (!finalText) return;

    // Add to UI/Conversation
    conversationController.addUserTurn(finalText);
  }

  private setupSessionEvents(session: RealtimeSession) {
    session.on('transport_event', async (event: any) => {
      const t = event.type || '';
      
      // Forward to VoiceController
      if (t === 'conversation.item.input_audio_transcription.delta') {
        voiceController.handleTranscript(event.delta || '', false);
      }
      if (t === 'conversation.item.input_audio_transcription.completed') {
        voiceController.handleTranscript(event.transcript || '', true);
      }
      if (t === 'input_audio_buffer.speech_started') {
        voiceController.handleSpeechStart();
      }
      if (t === 'input_audio_buffer.speech_stopped') {
        voiceController.handleSpeechStop();
      }

      // Streaming Response
      if (t.startsWith('response.output_audio') || t === 'response.output_text.delta') {
        const delta = event.delta || '';
        if (delta) {
          conversationController.appendStreaming(delta);
          uiController.updateButtonState(VoiceButtonState.RESPONDING);
        }
      }

      // Audio Monitoring
      if (t.startsWith('response.output_audio')) {
        void audioController.startSpeakerMonitor();
      }

      // Response Completion
      if (t === 'response.done') {
        if (conversationController.isStreaming()) {
          conversationController.finalizeStreaming();
          uiController.updateButtonState(VoiceButtonState.READY);
        }
      }
      
      // Item handling
      if (t === 'conversation.item.added') {
        conversationController.handleItemAdded(event);
      }
      if (t === 'conversation.item.done') {
        conversationController.handleItemDone(event);
      }
      
      // Error handling
      if (t === 'error') {
        logger.error('Session error event', event);
        uiEnhancements.showToast('Session error occurred', 'error');
      }
    });
  }

  private async getSessionToken(): Promise<string> {
    const r = await fetch(`${CONFIG.API_BASE}/session`);
    if (!r.ok) throw new Error('Failed to get session token');
    const js = await r.json();
    return js.value || js.client_secret?.value;
  }
}

export const appController = new AppController();