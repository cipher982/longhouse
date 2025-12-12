/**
 * App Controller
 * High-level orchestrator for the Jarvis Application.
 * Coordinates Audio, Voice, Session, and State.
 *
 * NOTE: This controller does NOT manipulate the DOM directly.
 * All UI updates are done via stateManager events → React hooks → React state.
 */

import { type RealtimeSession } from '@openai/agents/realtime';
import { logger, getJarvisClient, SessionManager } from '@jarvis/core';
import type { ConversationTurn } from '@jarvis/data-local';
import { stateManager } from './state-manager';
import { getZergApiUrl } from './config';
import { sessionHandler } from './session-handler';
import { bootstrapSession, type BootstrapResult } from './session-bootstrap';
import { audioController } from './audio-controller';
import { voiceController, type VoiceEvent } from './voice-controller';
import { conversationController } from './conversation-controller';
import { feedbackSystem } from './feedback-system';
import { CONFIG, buildConversationManagerOptions } from './config';
import { TextChannelController } from './text-channel-controller';
import { createContextTools } from './tool-factory';
import { contextLoader } from '../contexts/context-loader';

export class AppController {
  private initialized = false;
  private connecting = false;
  private textChannelController: TextChannelController | null = null;
  private lastBootstrapResult: BootstrapResult | null = null;

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

    // 1. Initialize JarvisClient for Zerg backend communication
    await this.initializeJarvisClient();

    // 2. Fetch bootstrap configuration from server
    await this.fetchBootstrap();

    // 3. Load context (required for voice/text sessions)
    await this.initializeContext();

    // 4. Setup Event Listeners
    this.setupVoiceListeners();

    // 5. Initialize Text Channel Controller
    this.textChannelController = new TextChannelController({
      autoConnect: true,
      maxRetries: 3
    });
    this.textChannelController.setVoiceController(voiceController);
    this.textChannelController.setConnectCallback(this.connect);

    // 6. Async initialization
    await this.textChannelController.initialize();

    this.initialized = true;
    logger.info('✅ App Controller initialized');
  }

  /**
   * Initialize the JarvisClient for Zerg backend communication
   */
  private async initializeJarvisClient(): Promise<void> {
    try {
      const zergApiUrl = getZergApiUrl();
      logger.info(`🔌 Initializing JarvisClient with URL: ${zergApiUrl}`);

      const jarvisClient = getJarvisClient(zergApiUrl);
      stateManager.setJarvisClient(jarvisClient);

      // SaaS model: Jarvis uses the same auth as the dashboard (JWT bearer token in localStorage).
      // If not logged in, supervisor features will fail with 401 when invoked.
      if (jarvisClient.isAuthenticated()) {
        logger.info('✅ JarvisClient token detected (zerg_jwt)');
      } else {
        logger.warn('⚠️ No zerg_jwt token detected - log in to enable supervisor features');
      }
    } catch (error) {
      logger.error('❌ Failed to initialize JarvisClient:', error);
      // Non-fatal - supervisor features will be unavailable but voice still works
    }
  }

  /**
   * Fetch bootstrap configuration from server
   */
  private async fetchBootstrap(): Promise<void> {
    try {
      logger.info('🔄 Fetching bootstrap configuration from server...');
      const token = typeof window !== 'undefined' ? window.localStorage.getItem('zerg_jwt') : null;
      const headers: Record<string, string> = {};
      if (token) {
        headers.Authorization = `Bearer ${token}`;
      }
      const response = await fetch(`${CONFIG.JARVIS_API_BASE}/bootstrap`, { headers });

      if (!response.ok) {
        throw new Error(`Bootstrap fetch failed: ${response.status} ${response.statusText}`);
      }

      const bootstrap = await response.json();
      stateManager.setBootstrap(bootstrap);

      logger.info('✅ Bootstrap configuration loaded from server');
      logger.debug('Bootstrap prompt length:', bootstrap.prompt?.length);
      logger.debug('Bootstrap tools:', bootstrap.enabled_tools?.map((t: any) => t.name));
    } catch (error) {
      logger.error('❌ Failed to fetch bootstrap configuration:', error);
      // Non-fatal - will fall back to client-side generated instructions
    }
  }

  /**
   * Initialize context and session manager
   */
  private async initializeContext(): Promise<void> {
    try {
      // Auto-detect and load context (defaults to 'personal')
      const contextName = await contextLoader.autoDetectContext();
      logger.info(`📋 Loading context: ${contextName}`);

      const currentContext = await contextLoader.loadContext(contextName);
      stateManager.setContext(currentContext);

      // Initialize Session Manager for this context (same as main.ts)
      const sessionManager = new SessionManager({}, {
        conversationManagerOptions: buildConversationManagerOptions(currentContext),
        maxHistoryTurns: currentContext.settings?.maxHistoryTurns ?? 50,
      });
      stateManager.setSessionManager(sessionManager);
      conversationController.setSessionManager(sessionManager);

      // Initialize session
      const initialConversationId = await sessionManager.initializeSession(currentContext, contextName);
      stateManager.setConversationId(initialConversationId);
      conversationController.setConversationId(initialConversationId);

      // Load history immediately for UI (don't wait for Realtime connection)
      const history = await sessionManager.getConversationHistory();
      if (history.length > 0) {
        stateManager.historyLoaded(history);
      }

      logger.info(`✅ Context initialized: ${contextName}`);
    } catch (error) {
      logger.error('❌ Failed to initialize context:', error);
      throw error; // Context is required, so we propagate the error
    }
  }

  /**
   * Get the last bootstrap result (for accessing history after connect)
   */
  getLastBootstrapResult(): BootstrapResult | null {
    return this.lastBootstrapResult;
  }

  /**
   * Connect to Voice Session
   * Uses bootstrapSession for SSOT - history is loaded once and provided to both
   * UI (via callback) and Realtime (via hydration)
   */
  async connect(): Promise<void> {
    if (this.connecting) return;
    this.connecting = true;

    logger.info('🔗 Connect sequence starting...');

    try {
      stateManager.setVoiceStatus('connecting');

      const currentContext = stateManager.getState().currentContext;
      const sessionManager = stateManager.getState().sessionManager;

      // 1. Acquire Microphone (via AudioController)
      const micStream = await audioController.requestMicrophone();

      // PRIVACY-CRITICAL: Mute immediately
      audioController.muteMicrophone();

      // 2. Validate prerequisites
      if (!currentContext) {
        throw new Error('No active context loaded');
      }
      if (!sessionManager) {
        throw new Error('No session manager available');
      }

      // Create tools using factory
      const tools = createContextTools(currentContext, sessionManager);

      // 3. Bootstrap session with SSOT history
      // This loads history ONCE and provides it to both UI and Realtime
      const bootstrapResult = await bootstrapSession({
        context: currentContext,
        sessionManager,
        mediaStream: micStream,
        audioElement: undefined,
        tools,
        onTokenRequest: this.getSessionToken,
        realtimeHistoryTurns: currentContext.settings?.realtimeHistoryTurns ?? 8,
      });

      const { session, agent, history, conversationId, hydratedItemCount } = bootstrapResult;
      this.lastBootstrapResult = bootstrapResult;

      logger.info(`📜 SSOT Bootstrap: ${history.length} turns loaded, ${hydratedItemCount} items hydrated`);

      // 4. Notify UI with history via stateManager event
      // This is the SAME data used for Realtime hydration
      if (history.length > 0) {
        stateManager.historyLoaded(history);
      }

      // 5. Update State
      stateManager.setSession(session);
      stateManager.setAgent(agent);
      if (conversationId) {
        stateManager.setConversationId(conversationId);
        conversationController.setConversationId(conversationId);
      }

      // 6. Wire up Session Events
      this.setupSessionEvents(session);

      // 7. Wire up Controllers
      voiceController.setSession(session);
      voiceController.setMicrophoneStream(micStream);
      this.textChannelController?.setSession(session);

      // 8. Finalize State
      stateManager.setVoiceStatus('ready');
      this.connecting = false;

      // Set voice mode ready state after connection
      voiceController.transitionToVoice({ handsFree: false });

      // Audio feedback
      feedbackSystem.playConnectChime();
      stateManager.showToast('Connected successfully', 'success');

    } catch (error: any) {
      logger.error('Connection failed', error);
      this.connecting = false;

      // Cleanup
      audioController.releaseMicrophone();
      stateManager.setVoiceStatus('error');
      stateManager.setConnectionError(error);
      stateManager.showToast(`Connection failed: ${error.message}`, 'error');
      feedbackSystem.playErrorTone();
    }
  }

  /**
   * Send a text message to the session
   */
  async sendText(text: string): Promise<void> {
    if (!this.textChannelController) {
      throw new Error('Text channel not initialized');
    }
    await this.textChannelController.sendText(text);
  }

  /**
   * Disconnect from Voice Session
   */
  async disconnect(): Promise<void> {
    logger.info('🔌 Disconnect sequence starting...');

    stateManager.showToast('Disconnecting...', 'info');
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
      stateManager.showToast('Disconnected', 'info');

    } catch (error) {
      logger.error('Disconnect error', error);
    } finally {
      stateManager.setVoiceStatus('idle');
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
          stateManager.showToast(`Voice error: ${event.error.message}`, 'error');
          break;
      }
    });
  }

  private handleVoiceStateChange(state: any) {
    // Handle audio feedback via AudioController
    // VAD active means we should visualize listening
    if (state.vadActive || state.active) {
      audioController.setListeningMode(true).catch(() => {});
      stateManager.setVoiceStatus('listening');
    } else {
      audioController.setListeningMode(false).catch(() => {});
      // If connected but not active, set to READY
      if (voiceController.isConnected()) {
        stateManager.setVoiceStatus('ready');
      }
    }

    // NOTE: Mic muting/unmuting is handled exclusively by voiceController
    // It manages track.enabled directly in startPTT(), stopPTT(), setHandsFree(), handleVADStateChange()
    // Do NOT add duplicate mic control here - it causes conflicts
  }

  private async handleUserTranscript(text: string): Promise<void> {
    const finalText = text.trim();
    if (!finalText) return;

    // Add to conversation controller (for persistence)
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

      // Streaming Response - handle text deltas from various event types
      // The OpenAI Agents SDK may emit different event names:
      // - response.output_text.delta: Text mode responses (SDK naming)
      // - response.output_audio_transcript.delta: Audio with transcript (actual event from API)
      // - response.audio_transcript.delta: Voice transcript (alternative naming)
      // - response.text.delta: Alternative text delta
      const isTextDelta =
        t === 'response.output_text.delta' ||
        t === 'response.output_audio_transcript.delta' ||
        t === 'response.audio_transcript.delta' ||
        t === 'response.text.delta';

      if (isTextDelta) {
        // Prefer transcript field, fall back to delta
        const text = event.transcript || event.delta || '';
        if (text && typeof text === 'string') {
          conversationController.appendStreaming(text);
          stateManager.setVoiceStatus('speaking');
        }
      }

      // Track when audio output starts (for visual feedback)
      if (t.startsWith('response.output_audio') || t.startsWith('response.audio')) {
        stateManager.setVoiceStatus('speaking');
        void audioController.startSpeakerMonitor();
      }

      // Response Completion - ALWAYS reset status, finalize if streaming
      if (t === 'response.done') {
        if (conversationController.isStreaming()) {
          conversationController.finalizeStreaming();
        }
        // Always reset to ready when response is complete
        stateManager.setVoiceStatus('ready');
      }

      // Item handling
      if (t === 'conversation.item.added') {
        conversationController.handleItemAdded(event);
      }
      if (t === 'conversation.item.done') {
        conversationController.handleItemDone(event);

        // User voice committed - add placeholder immediately for correct ordering
        const item = event.item;
        if (item?.role === 'user' && item?.id) {
          const contentType = item.content?.[0]?.type;
          if (contentType === 'input_audio') {
            stateManager.userVoiceCommitted(item.id);
          }
        }
      }

      // User voice transcript ready - update placeholder with actual text
      if (t === 'conversation.item.input_audio_transcription.completed') {
        // Find the item ID from the event (it references the user message item)
        const itemId = event.item_id;
        const transcript = event.transcript || '';
        if (itemId && transcript) {
          stateManager.userVoiceTranscript(itemId, transcript);
        }
      }

      // Error handling
      if (t === 'error') {
        const errorMsg = event.error?.message || event.error?.code || 'Unknown error';
        logger.error('Session error event', { type: t, error: event.error, message: errorMsg });
        stateManager.showToast(`Session error: ${errorMsg}`, 'error');
      }
    });
  }

  private async getSessionToken(): Promise<string> {
    const token = typeof window !== 'undefined' ? window.localStorage.getItem('zerg_jwt') : null;
    const headers: Record<string, string> = {};
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
    const r = await fetch(`${CONFIG.JARVIS_API_BASE}/session`, { headers });
    if (!r.ok) throw new Error('Failed to get session token');
    const js = await r.json();
    return js.value || js.client_secret?.value;
  }
}

export const appController = new AppController();
