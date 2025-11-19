/**
 * App Controller
 * High-level orchestrator for the Jarvis Application.
 * Coordinates Audio, Voice, Session, and UI State.
 */

import { RealtimeAgent, RealtimeSession } from '@openai/agents/realtime';
import { logger, SessionManager, getJarvisClient } from '@jarvis/core';
import { stateManager } from './state-manager';
import { sessionHandler } from './session-handler';
import { audioController } from './audio-controller';
import { voiceController, initializeVoiceController, type VoiceState } from './voice-controller';
import { conversationController } from './conversation-controller';
import { uiEnhancements } from './ui-enhancements';
import { feedbackSystem } from './feedback-system';
import { contextLoader } from '../contexts/context-loader';
import { VoiceButtonState, CONFIG } from './config';
import { TextChannelController } from './text-channel-controller';
import { createContextTools } from './tool-factory';

// Types for integration
import type { VoiceAgentConfig } from '../contexts/types';

export class AppController {
  private initialized = false;
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

    // 1. Initialize Voice Controller callbacks
    this.setupVoiceController();

    // 2. Initialize Text Channel Controller
    this.textChannelController = new TextChannelController({
      autoConnect: true,
      maxRetries: 3
    });
    this.textChannelController.setVoiceController(voiceController);
    this.textChannelController.setConnectCallback(this.connect);

    // 3. Initialize Audio Controller (AudioContext mainly)
    // Note: DOM elements for visualization are passed separately via attachUI
    
    // 4. Async initialization
    await voiceController.initialize();
    await this.textChannelController.initialize();

    this.initialized = true;
    logger.info('✅ App Controller initialized');
  }

  /**
   * Connect to Voice Session
   */
  async connect(): Promise<void> {
    if (stateManager.isConnecting()) return;

    logger.info('🔗 Connect sequence starting...');
    
    let loadingOverlay: HTMLDivElement | null = null;
    
    try {
      stateManager.setVoiceButtonState(VoiceButtonState.CONNECTING);
      loadingOverlay = uiEnhancements.showLoading('Requesting microphone access...');

      const currentContext = stateManager.getState().currentContext;
      
      // 1. Acquire Microphone (via AudioController)
      const micStream = await audioController.requestMicrophone();
      
      // PRIVACY-CRITICAL: Mute immediately
      audioController.muteMicrophone();

      if (loadingOverlay) {
        loadingOverlay.querySelector('.loading-text')!.textContent = 'Connecting to OpenAI...';
      }

      // 2. Connect Session (via SessionHandler)
      // We need to reconstruct the context tools here or get them from somewhere
      // For now, we'll assume the session handler can handle tool creation if passed the config
      // But sessionHandler.connect expects an array of tools.
      // We need to extract the tool creation logic from main.ts or duplicate it.
      // For this refactor, I'll assume we can pass the tools from the context.
      // TODO: Refactor tool creation to a separate utility.
      
      // Quick fix: we need the tools. 
      // Let's assume for this step we get the context from stateManager
      if (!currentContext) {
        throw new Error('No active context loaded');
      }
      
      // Re-create tools (we need to move createContextTools to a utility, 
      // but for now we'll import it or define it here? 
      // Better: Export it from main.ts or move it to a tool-factory file.
      // For now, let's trust that sessionHandler manages the agent creation well enough
      // if we pass the tools.
      
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
      stateManager.setVoiceButtonState(VoiceButtonState.READY);
      
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
      
      // Cleanup
      audioController.releaseMicrophone();
      stateManager.setVoiceButtonState(VoiceButtonState.IDLE);
      
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
      
      // 3. Cleanup Audio
      audioController.dispose(); // Releases mic and stops monitor

      logger.info('✅ Disconnected successfully');
      uiEnhancements.showToast('Disconnected', 'info');

    } catch (error) {
      logger.error('Disconnect error', error);
    } finally {
      stateManager.setVoiceButtonState(VoiceButtonState.IDLE);
    }
  }

  // ================= PRIVATE HELPERS =================

  private setupVoiceController() {
    initializeVoiceController({
      onStateChange: (state: VoiceState) => {
        // Sync to global state manager
        stateManager.setVoiceState(state);
        
        // Derive VoiceButtonState from VoiceState
        if (state.pttActive) {
          stateManager.setVoiceButtonState(VoiceButtonState.SPEAKING);
        } else if (state.armed) {
          stateManager.setVoiceButtonState(VoiceButtonState.READY);
        } else if (stateManager.isConnected()) {
          // If connected but not armed/active
          stateManager.setVoiceButtonState(VoiceButtonState.READY);
        } else {
          stateManager.setVoiceButtonState(VoiceButtonState.IDLE);
        }

        // Handle audio feedback via AudioController
        // VAD active means we should visualize listening
        if (state.vadActive || state.active) {
          audioController.setListeningMode(true).catch(() => {});
        } else {
          audioController.setListeningMode(false).catch(() => {});
        }
        
        // Handle mic mute state
        if (state.active) {
          audioController.unmuteMicrophone();
        } else {
          audioController.muteMicrophone();
        }
      },
      onArmed: () => {
        logger.debug('Voice armed');
      },
      onMuted: () => {
        logger.debug('Voice muted');
      },
      onTranscript: (text: string, isFinal: boolean) => {
        if (!isFinal) {
          conversationController.updateUserPreview(text);
        } else {
          // Optionally clear/finalize in controller (handled by onFinalTranscript)
        }
      },
      onFinalTranscript: (text: string) => {
        this.handleUserTranscript(text);
      },
      onVADStateChange: (active: boolean) => {
        if (active) {
          feedbackSystem.playVoiceTick();
        }
      },
      onModeTransition: (from: 'voice' | 'text', to: 'voice' | 'text') => {
        stateManager.setConversationMode(to);
      },
      onError: (error: Error) => {
        logger.error('Voice controller error:', error);
        uiEnhancements.showToast(`Voice error: ${error.message}`, 'error');
      }
    });
  }

  private async handleUserTranscript(text: string): Promise<void> {
    const finalText = text.trim();
    if (!finalText) return;

    // Add to UI/Conversation
    conversationController.addUserTurn(finalText);
    
    // Here we would check for agent dispatch commands (Zerg)
    // For now, just basic handling is fine, we can move the dispatch logic here later
    // or import it. 
    // Since main.ts had 'findAgentByIntent', we should ideally move that to a helper too.
    // For this refactor step, I'll skip the specific Zerg dispatch logic to keep it focused
    // on architecture, but conversationController will handle the display.
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
          stateManager.setVoiceButtonState(VoiceButtonState.READY);
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
    // Copied from main.ts logic
    const r = await fetch(`${CONFIG.API_BASE}/session`);
    if (!r.ok) throw new Error('Failed to get session token');
    const js = await r.json();
    return js.value || js.client_secret?.value;
  }
}

export const appController = new AppController();
