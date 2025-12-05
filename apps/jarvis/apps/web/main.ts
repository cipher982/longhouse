// Jarvis PWA - Refactored Entry Point
import { logger, getJarvisClient, SessionManager } from '@jarvis/core';
import { createTaskInbox, type TaskInbox } from './lib/task-inbox';
import { contextLoader } from './contexts/context-loader';
import { uiEnhancements } from './lib/ui-enhancements';
import { ConversationRenderer } from './lib/conversation-renderer';
import { ConversationUI } from './lib/conversation-ui';
import { supervisorProgress } from './lib/supervisor-progress';
import { buildConversationManagerOptions } from './lib/config';
import './lib/test-helpers'; // Load test helpers for e2e testing

// Controllers & State
import { appController } from './lib/app-controller';
import { audioController } from './lib/audio-controller';
import { stateManager, type StateChangeEvent } from './lib/state-manager';
import { voiceController, initializeVoiceController } from './lib/voice-controller';
import { conversationController } from './lib/conversation-controller';
import { feedbackSystem } from './lib/feedback-system';
import { VoiceButtonState } from './lib/config';
import type { VoiceAgentConfig } from './contexts/types';

// Global UI elements
let pttBtn: HTMLButtonElement | undefined;
let transcriptEl: HTMLDivElement | undefined;
let sidebar: HTMLDivElement | undefined;
let sidebarToggle: HTMLButtonElement | undefined;
let voiceStatusText: HTMLSpanElement | undefined;
let remoteAudio: HTMLAudioElement | undefined;
let handsFreeToggle: HTMLButtonElement | undefined;

function createSessionManagerForContext(config: VoiceAgentConfig): SessionManager {
  return new SessionManager({}, {
    conversationManagerOptions: buildConversationManagerOptions(config),
    maxHistoryTurns: config.settings.maxHistoryTurns
  });
}

// Initialize page
document.addEventListener("DOMContentLoaded", async () => {
  console.log('🚀 Initializing Jarvis PWA...');

  // 1. Initialize DOM references
  transcriptEl = document.getElementById("transcript") as HTMLDivElement;
  pttBtn = document.getElementById("pttBtn") as HTMLButtonElement;
  const newConversationBtn = document.getElementById("newConversationBtn") as HTMLButtonElement;
  const clearConvosBtn = document.getElementById("clearConvosBtn") as HTMLButtonElement;
  const syncNowBtn = document.getElementById("syncNowBtn") as HTMLButtonElement;
  sidebarToggle = document.getElementById("sidebarToggle") as HTMLButtonElement;
  sidebar = document.getElementById("sidebar") as HTMLDivElement;
  voiceStatusText = document.querySelector('.voice-status-text') as HTMLSpanElement;
  handsFreeToggle = document.getElementById('handsFreeToggle') as HTMLButtonElement;
  remoteAudio = document.getElementById('remoteAudio') as HTMLAudioElement | null || undefined;
  const textInput = document.getElementById('textInput') as HTMLInputElement;
  const sendTextBtn = document.getElementById('sendTextBtn');
  
  // 2. Initialize UI Components
  const conversationRenderer = new ConversationRenderer(transcriptEl);
  conversationController.setRenderer(conversationRenderer);

  const conversationUI = new ConversationUI();

  // 2.5. Initialize Supervisor Progress UI (for complex task delegation)
  supervisorProgress.initialize('supervisor-progress');
  
  // 3. Initialize Audio Controller (Visualizer + Remote Audio)
  if (pttBtn && remoteAudio) {
    audioController.initialize(remoteAudio, pttBtn);
  }

  // 3.5. Initialize Voice Controller (must happen before App Controller)
  initializeVoiceController({});

  // 4. Initialize App Controller (Core Logic)
  await appController.initialize();

  // 4.5. Expose controllers to window for testing
  if (typeof window !== 'undefined') {
    (window as any).voiceController = voiceController;
    (window as any).audioController = audioController;
    (window as any).appController = appController;
  }
  
  // 5. Wire up State Listeners (Reactive UI) - Direct Subscription Pattern
  
  // Voice State (Input)
  voiceController.addListener((event) => {
    if (event.type === 'stateChange') {
      renderButtonState();
    }
  });

  // Conversation State (Output)
  conversationController.addListener((event) => {
    if (event.type === 'streamingStart' || event.type === 'streamingStop') {
      renderButtonState();
    }
  });

  // 6. Event Handlers

  // PTT Button
  if (pttBtn) {
    // Click to connect
    pttBtn.addEventListener('click', async (e) => {
      feedbackSystem.resumeContext().catch(() => {});

      // If not connected, connect
      if (!voiceController.isConnected()) {
        updateVoiceButtonUI(VoiceButtonState.CONNECTING);
        try {
          await appController.connect();
          // Success will trigger voice state change -> renderButtonState
        } catch (error: any) {
          console.error('Connection failed:', error);
          updateVoiceButtonUI(VoiceButtonState.IDLE);
          // Show visible error to user
          setStatusLabel(`❌ Error: ${error.message || 'Connection failed'}`);
          setTimeout(() => {
            setStatusLabel('Start voice session');
          }, 5000);
        }
      }
    });

    // Hold to talk
    const startTalking = () => {
      const state = voiceController.getState();
      if (voiceController.isConnected() && voiceController.isVoiceMode() && !state.handsFree) {
        voiceController.startPTT();
      }
    };

    const stopTalking = () => {
      const state = voiceController.getState();
      if (voiceController.isConnected() && state.pttActive) {
        voiceController.stopPTT();
      }
    };

    pttBtn.addEventListener('mousedown', startTalking);
    pttBtn.addEventListener('touchstart', (e) => { e.preventDefault(); startTalking(); });
    pttBtn.addEventListener('mouseup', stopTalking);
    pttBtn.addEventListener('mouseleave', stopTalking);
    pttBtn.addEventListener('touchend', stopTalking);
    
    // Keyboard PTT
    voiceController.setupKeyboardHandlers(pttBtn);
    pttBtn.onkeydown = async (e: KeyboardEvent) => {
      if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        feedbackSystem.resumeContext().catch(() => {});
        
        if (!voiceController.isConnected()) {
           updateVoiceButtonUI(VoiceButtonState.CONNECTING);
           try {
             await appController.connect();
           } catch {
             updateVoiceButtonUI(VoiceButtonState.IDLE);
           }
           return;
        }
        
        if (voiceController.isVoiceMode()) {
          voiceController.startPTT();
        }
      }
    };
    pttBtn.onkeyup = (e: KeyboardEvent) => {
      if (e.key === ' ' || e.key === 'Enter') {
        voiceController.stopPTT();
      }
    };
  }

  // Sidebar Actions
  if (newConversationBtn) {
    newConversationBtn.onclick = async () => {
      // Delegate to session manager via state
      const sm = stateManager.getState().sessionManager;
      if (sm) {
        if (voiceController.isConnected()) await appController.disconnect();
        const id = await sm.createNewConversation();
        conversationController.setConversationId(id);
        await conversationController.loadHistory();
        conversationUI.addNewConversation(id);
      }
    };
  }

  if (clearConvosBtn) {
    clearConvosBtn.onclick = async () => {
      const sm = stateManager.getState().sessionManager;
      if (sm && confirm('Delete all conversations?')) {
        await sm.clearAllConversations();
        conversationUI.clearConversations();
        conversationController.clear();
      }
    };
  }

  // Text Input Handlers
  if (textInput && sendTextBtn) {
    const handleTextSend = async () => {
      const text = textInput.value.trim();
      if (text) {
        textInput.value = '';
        conversationController.addUserTurn(text);
        try {
          await appController.sendText(text);
        } catch (error: any) {
          console.error('Failed to send text:', error);
          uiEnhancements.showToast(`Failed to send: ${error.message}`, 'error');
        }
      }
    };

    sendTextBtn.addEventListener('click', handleTextSend);
    textInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleTextSend();
      }
    });
  }

  // Hands-free Toggle
  if (handsFreeToggle) {
    handsFreeToggle.addEventListener('click', () => {
      const wasEnabled = handsFreeToggle!.getAttribute('aria-checked') === 'true';
      const enabled = !wasEnabled;
      handsFreeToggle!.setAttribute('aria-checked', enabled.toString());
      
      if (enabled && voiceController.isTextMode()) {
        voiceController.transitionToVoice({ handsFree: false });
      }
      voiceController.setHandsFree(enabled);
      uiEnhancements.showToast(enabled ? 'Hands-free enabled' : 'Hands-free disabled', 'info');
    });
  }
  
  // Context Loading (Legacy logic adapted)
  try {
    // Initialize context system
    const contextName = await contextLoader.autoDetectContext();
    const currentContext = await contextLoader.loadContext(contextName);
    stateManager.setContext(currentContext);
    
    updateUIForContext(currentContext);
    contextLoader.createContextSelector('context-selector-container');
    
    // Initialize Session Manager
    const sessionManager = createSessionManagerForContext(currentContext);
    stateManager.setSessionManager(sessionManager);
    conversationController.setSessionManager(sessionManager);
    conversationUI.setSessionManager(sessionManager);
    
    // Load conversation
    const initialConversationId = await sessionManager.initializeSession(currentContext, contextName);
    stateManager.setConversationId(initialConversationId);
    conversationController.setConversationId(initialConversationId);
    
    await conversationUI.loadConversations();
    if (initialConversationId) {
      conversationUI.setActiveConversation(initialConversationId);
      await conversationController.loadHistory();
    }
    
  } catch (error) {
    console.error('Context load failed:', error);
    // Fallback or error handling
  }
  
  // Initialize Zerg Integration (Task Inbox)
  try {
    const taskInboxContainer = document.getElementById('task-inbox-container');
    if (taskInboxContainer) {
      await createTaskInbox(taskInboxContainer, {
        apiURL: import.meta.env?.VITE_ZERG_API_URL || 'http://localhost:47300',
        deviceSecret: import.meta.env?.VITE_JARVIS_DEVICE_SECRET || '',
        onError: (err) => console.error('TaskInbox error:', err),
        onRunUpdate: (run) => {
           if (run.status === 'success') {
             // Optional: Speak result
           }
        }
      });
    }
  } catch (e) {
    console.warn('Zerg integration failed', e);
  }

  // Auto-connect check
  const params = new URLSearchParams(window.location.search);
  if (params.get('autoconnect') === '1') {
    setTimeout(() => {
        updateVoiceButtonUI(VoiceButtonState.CONNECTING);
        appController.connect().catch(() => updateVoiceButtonUI(VoiceButtonState.IDLE));
    }, 500);
  }
});

// UI Helpers

/**
 * Derive and render button state from controllers
 */
function renderButtonState() {
    const voiceState = voiceController.getState();
    const isStreaming = conversationController.isStreaming();

    // Sync Hands-free toggle state
    if (handsFreeToggle) {
      const currentState = handsFreeToggle.getAttribute('aria-checked') === 'true';
      if (currentState !== voiceState.handsFree) {
        handsFreeToggle.setAttribute('aria-checked', voiceState.handsFree.toString());
      }
    }

    if (!voiceController.isConnected()) {
        return; 
    }

    let newState = VoiceButtonState.READY;

    if (isStreaming) {
        newState = VoiceButtonState.RESPONDING;
    } else if (voiceState.pttActive || (voiceState.vadActive && voiceState.handsFree)) {
        newState = VoiceButtonState.SPEAKING;
    } else if (voiceState.active) {
        newState = VoiceButtonState.SPEAKING; // or LISTENING
    } 

    updateVoiceButtonUI(newState);
}

function updateVoiceButtonUI(state: VoiceButtonState) {
  if (!pttBtn) return;
  
  // Clear classes
  pttBtn.classList.remove('idle', 'connecting', 'ready', 'speaking', 'listening', 'responding');
  
  // Add active class
  switch (state) {
    case VoiceButtonState.IDLE:
      pttBtn.classList.add('idle');
      setStatusLabel('Start voice session');
      setMicIcon(false);
      break;
    case VoiceButtonState.CONNECTING:
      pttBtn.classList.add('connecting');
      setStatusLabel('Connecting...');
      break;
    case VoiceButtonState.READY:
      pttBtn.classList.add('ready');
      setStatusLabel('🎙️ Ready - hold to talk');
      setMicIcon(false);
      break;
    case VoiceButtonState.SPEAKING:
      pttBtn.classList.add('speaking');
      setStatusLabel('🔴 Recording...');
      setMicIcon(true);
      break;
    case VoiceButtonState.RESPONDING:
      pttBtn.classList.add('responding');
      setStatusLabel('💬 Responding...');
      setMicIcon(false);
      break;
  }
}

function setStatusLabel(text: string) {
  if (voiceStatusText) voiceStatusText.textContent = text;
}

const MIC_ICON = `
  <path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/>
  <path d="M19 10v2a7 7 0 01-14 0v-2M12 19v4M8 23h8"/>
`;
const STOP_ICON = `
  <rect x="6" y="6" width="12" height="12" rx="2"/>
`;

function setMicIcon(listening: boolean) {
  const iconEl = pttBtn?.querySelector('.voice-icon');
  if (!iconEl) return;
  
  iconEl.innerHTML = listening ? STOP_ICON : MIC_ICON;
  if (listening) {
    iconEl.setAttribute('fill', 'currentColor');
    iconEl.setAttribute('stroke', 'none');
  } else {
    iconEl.setAttribute('fill', 'none');
    iconEl.setAttribute('stroke', 'currentColor');
    iconEl.setAttribute('stroke-width', '2');
  }
}

function updateUIForContext(config: any) {
  document.title = config.branding.title;
  const titleEl = document.getElementById('appTitle');
  if (titleEl) titleEl.textContent = config.branding.title;
}