// Jarvis PWA with OpenAI Agents SDK - Enhanced with conversation persistence
import { RealtimeAgent, RealtimeSession, OpenAIRealtimeWebRTC } from '@openai/agents/realtime';
import { tool } from '@openai/agents';
import { z } from 'zod';
import { SessionManager, logger, getJarvisClient, type JarvisAgentSummary } from '@jarvis/core';
import { ConversationUI } from './lib/conversation-ui';
import { ConversationRenderer } from './lib/conversation-renderer';
import { createTaskInbox, type TaskInbox } from './lib/task-inbox';
import type { ConversationTurn, ConversationManagerOptions, SyncTransport } from '@jarvis/data-local';
import { contextLoader } from './contexts/context-loader';
import type { VoiceAgentConfig } from './contexts/types';
import { uiEnhancements } from './lib/ui-enhancements';
import { RadialVisualizer } from './lib/radial-visualizer';
import { VoiceController, type VoiceState } from './lib/voice-controller';
import { TextChannelController } from './lib/text-channel-controller';
import {
  VoiceButtonState,
  CONFIG as MODULE_CONFIG,
  resolveSyncBaseUrl,
  createSyncTransport,
  buildConversationManagerOptions
} from './lib/config';

import { stateManager } from './lib/state-manager';
import { sessionHandler } from './lib/session-handler';
import { feedbackSystem } from './lib/feedback-system';
import { eventBus } from './lib/event-bus';
import { conversationController } from './lib/conversation-controller';

// Use the imported config (no duplication!)
const CONFIG = MODULE_CONFIG;

function createSessionManagerForContext(config: VoiceAgentConfig): SessionManager {
  return new SessionManager({}, {
    conversationManagerOptions: buildConversationManagerOptions(config),
    maxHistoryTurns: config.settings.maxHistoryTurns
  });
}


// Global state
let agent: RealtimeAgent | null = null;
let session: RealtimeSession | null = null;
let sessionManager: SessionManager | null = null;
let conversationUI: ConversationUI | null = null;
let conversationRenderer: ConversationRenderer | null = null;
// voiceButtonState now managed by stateManager
let currentContext: VoiceAgentConfig | null = null;
// Track a pending user bubble while transcription completes
// Legacy DOM element placeholders removed in favor of renderer-based state
// (avoid multiple writers / race conditions)

// Jarvis-Zerg integration
// Removed: conversationMode - now use voiceController.getState().interactionMode as source of truth
let taskInbox: TaskInbox | null = null;
let jarvisClient = getJarvisClient(import.meta.env?.VITE_ZERG_API_URL || 'http://localhost:47300');
let cachedAgents: JarvisAgentSummary[] = [];
// Track if transcript is showing status text (e.g., "Connecting…")
let statusActive = false;
// Accumulate partial transcription text when VAD is used (no PTT)
let pendingUserText = '';

// Voice/Text Separation Controllers (Phase 11 - Jarvis Voice/Text Separation)
let voiceController: VoiceController;
let textChannelController: TextChannelController;

// Haptic & Audio Feedback System (Phase 6)
interface FeedbackPreferences {
  haptics: boolean;
  audio: boolean;
}

// Load preferences from localStorage
function loadFeedbackPreferences(): FeedbackPreferences {
  try {
    const stored = localStorage.getItem('jarvis.feedback.preferences');
    if (stored) {
      return JSON.parse(stored);
    }
  } catch (error) {
    logger.warn('Failed to load feedback preferences', error);
  }
  // Default: enabled
  return { haptics: true, audio: true };
}

function saveFeedbackPreferences(prefs: FeedbackPreferences): void {
  try {
    localStorage.setItem('jarvis.feedback.preferences', JSON.stringify(prefs));
  } catch (error) {
    logger.warn('Failed to save feedback preferences', error);
  }
}

const feedbackPrefs = loadFeedbackPreferences();

// Haptic feedback helper
function triggerHaptic(pattern: number | number[]): void {
  if (!feedbackPrefs.haptics) return;
  if (!('vibrate' in navigator)) return;

  try {
    navigator.vibrate(pattern);
  } catch (error) {
    // Silently fail if vibration not supported
  }
}

// Use feedbackSystem singleton from module
const audioFeedback = feedbackSystem;

// Centralized State Handler (Single Source of Truth)
function setVoiceButtonState(newState: VoiceButtonState): void {
  const currentState = stateManager.getState().voiceButtonState;
  if (currentState === newState) return; // No change needed
  if (!pttBtn || !voiceButtonContainer) return; // Guard against early calls before DOM ready

  const oldState = currentState;
  stateManager.setVoiceButtonState(newState);

  // Remove all CSS state classes from button (CSS targets .voice-button.ready etc)
  pttBtn.classList.remove('idle', 'connecting', 'ready', 'speaking', 'listening', 'responding');

  // Map VoiceButtonState enum to CSS classes that stylesheet expects
  // CSS expects: .voice-button.idle, .voice-button.ready, .voice-button.speaking, etc
  switch (newState) {
    case VoiceButtonState.IDLE:
      pttBtn.classList.add('idle');
      break;
    case VoiceButtonState.CONNECTING:
      pttBtn.classList.add('connecting');
      break;
    case VoiceButtonState.READY:
      pttBtn.classList.add('ready');
      break;
    case VoiceButtonState.SPEAKING:
    case VoiceButtonState.ACTIVE:
      pttBtn.classList.add('speaking'); // CSS uses 'speaking' for active mic
      break;
    case VoiceButtonState.RESPONDING:
      pttBtn.classList.add('responding');
      break;
    case VoiceButtonState.PROCESSING:
      pttBtn.classList.add('connecting'); // Use connecting style for processing
      break;
  }

  // Update ARIA attributes and status label for screen readers
  switch (newState) {
    case VoiceButtonState.IDLE:
      pttBtn.setAttribute('aria-label', 'Connect to voice service');
      pttBtn.setAttribute('aria-pressed', 'false');
      pttBtn.removeAttribute('aria-busy');
      setStatusLabel('Tap to speak');
      break;
    case VoiceButtonState.CONNECTING:
      pttBtn.setAttribute('aria-label', 'Connecting...');
      pttBtn.setAttribute('aria-busy', 'true');
      pttBtn.setAttribute('aria-pressed', 'false');
      setStatusLabel('Connecting...');
      break;
    case VoiceButtonState.READY:
      pttBtn.setAttribute('aria-label', 'Push to talk');
      pttBtn.setAttribute('aria-pressed', 'false');
      pttBtn.removeAttribute('aria-busy');
      setStatusLabel('Ready to talk');
      break;
    case VoiceButtonState.SPEAKING:
      pttBtn.setAttribute('aria-label', 'Speaking - release to send');
      pttBtn.setAttribute('aria-pressed', 'true');
      pttBtn.removeAttribute('aria-busy');
      setStatusLabel('Listening...');
      break;
    case VoiceButtonState.RESPONDING:
      pttBtn.setAttribute('aria-label', 'Assistant is responding');
      pttBtn.setAttribute('aria-pressed', 'false');
      pttBtn.removeAttribute('aria-busy');
      setStatusLabel('Assistant is responding');
      break;
  }

  logger.debug(`Voice button state transition: ${oldState} → ${newState}`);
}

// Status Text Helper - updates integrated button status
function setStatusLabel(text: string | null): void {
  if (!voiceStatusText) return;
  voiceStatusText.textContent = text || 'Tap to talk';
}

function clearStatusLabel(): void {
  setStatusLabel(null);
}

// State check helpers (replace boolean flags) - delegating to stateManager
function isConnected(): boolean {
  const state = stateManager.getState().voiceButtonState;
  return state !== VoiceButtonState.IDLE &&
         state !== VoiceButtonState.CONNECTING;
}

function isConnecting(): boolean {
  return stateManager.isConnecting();
}

function canStartPTT(): boolean {
  return stateManager.isReady();
}

// DOM elements - will be initialized in DOMContentLoaded
let transcriptEl: HTMLDivElement;
let pttBtn: HTMLButtonElement;
let voiceButtonContainer: HTMLButtonElement; // Same as pttBtn
let newConversationBtn: HTMLButtonElement;
let clearConvosBtn: HTMLButtonElement;
let syncNowBtn: HTMLButtonElement;
let sidebarToggle: HTMLButtonElement;
let sidebar: HTMLDivElement;
let voiceStatusText: HTMLSpanElement;
let handsFreeToggle: HTMLButtonElement;

const remoteAudio = document.getElementById('remoteAudio') as HTMLAudioElement | null;
let sharedMicStream: MediaStream | null = null;

enum AudioState {
  IDLE = 0,
  MIC_ACTIVE = 1 << 0,
  ASSISTANT_SPEAKING = 1 << 1,
}

const AUDIO_STATE_CLASSNAMES = ['audio-state-0', 'audio-state-1', 'audio-state-2', 'audio-state-3'];

let audioState: AudioState = AudioState.IDLE;
let micLevel = 0;
let speakerLevel = 0;
let speakerAudioCtx: AudioContext | null = null;
let speakerAnalyser: AnalyserNode | null = null;
let speakerDataArray: Uint8Array | null = null;
let speakerSource: MediaStreamAudioSourceNode | MediaElementAudioSourceNode | null = null;
let speakerStream: MediaStream | null = null;
let speakerRafId: number | null = null;
let speakerSilenceFrames = 0;
let speakerMonitorUnavailable = false;

const MIC_COLOR = '#ec4899';
const ASSISTANT_COLOR = '#3b82f6';
const BOTH_COLOR = '#a855f7';
const IDLE_COLOR = '#475569';

// Audio visualizer (radial) around mic
// Initialized in DOMContentLoaded
let radialViz: RadialVisualizer | null = null;

syncAudioStateClasses();
updateAudioVisualization();

// Small UI helpers (dedup repeated blocks)
const MIC_ICON = `
  <path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/>
  <path d="M19 10v2a7 7 0 01-14 0v-2M12 19v4M8 23h8"/>
`;
const STOP_ICON = `
  <rect x="6" y="6" width="12" height="12" rx="2"/>
`;

function setMicIcon(listening: boolean) {
  // CRITICAL: Do NOT use innerHTML on button - it destroys child elements!
  // Update only the .voice-icon SVG content to preserve other elements
  const iconEl = pttBtn?.querySelector('.voice-icon');
  if (!iconEl) return;

  iconEl.innerHTML = listening ? STOP_ICON : MIC_ICON;

  // Update SVG attributes based on icon type
  if (listening) {
    iconEl.setAttribute('fill', 'currentColor');
    iconEl.setAttribute('stroke', 'none');
  } else {
    iconEl.setAttribute('fill', 'none');
    iconEl.setAttribute('stroke', 'currentColor');
    iconEl.setAttribute('stroke-width', '2');
  }
}

function hasAudioState(flag: AudioState): boolean {
  return (audioState & flag) === flag;
}

function syncAudioStateClasses(): void {
  const classList = document.body.classList;
  for (const cls of AUDIO_STATE_CLASSNAMES) {
    classList.remove(cls);
  }
  classList.add(`audio-state-${audioState}`);
}

function updateAudioVisualization(): void {
  const micActive = hasAudioState(AudioState.MIC_ACTIVE);
  const assistantActive = hasAudioState(AudioState.ASSISTANT_SPEAKING);

  let level = 0;
  if (micActive && assistantActive) {
    level = Math.max(micLevel, speakerLevel);
  } else if (micActive) {
    level = micLevel;
  } else if (assistantActive) {
    level = speakerLevel;
  }

  const color = micActive && assistantActive
    ? BOTH_COLOR
    : micActive
      ? MIC_COLOR
      : assistantActive
        ? ASSISTANT_COLOR
        : IDLE_COLOR;

  if (radialViz) {
    radialViz.render(level, color, audioState);
  }

  if (voiceButtonContainer) {
    voiceButtonContainer.dataset.state = `${audioState}`;
    // Visual feedback for mic/assistant audio levels is handled via CSS state classes
    // The actual visual states (idle, ready, speaking, listening, responding) are managed elsewhere
  }
}

function setAudioStateValue(next: AudioState): void {
  if (audioState === next) return;
  audioState = next;
  syncAudioStateClasses();
  updateAudioVisualization();
}

function setAudioStateFlag(flag: AudioState, enabled: boolean): void {
  const next = enabled ? (audioState | flag) : (audioState & ~flag);
  setAudioStateValue(next as AudioState);
}

function handleMicLevel(level: number): void {
  micLevel = level;
  updateAudioVisualization();
}

function handleSpeakerLevel(level: number): void {
  speakerLevel = level;
  updateAudioVisualization();
}

function setAssistantSpeakingState(active: boolean): void {
  setAudioStateFlag(AudioState.ASSISTANT_SPEAKING, active);
}

async function startSpeakerMonitor(): Promise<void> {
  if (!remoteAudio || speakerMonitorUnavailable) return;

  try {
    if (!speakerAudioCtx) {
      speakerAudioCtx = new (window.AudioContext || (window as any).webkitAudioContext)();
    }

    if (speakerAudioCtx.state === 'suspended') {
      await speakerAudioCtx.resume().catch(() => undefined);
    }

    if (!speakerSource) {
      try {
        if (typeof (remoteAudio as any).captureStream === 'function') {
          speakerStream = (remoteAudio as any).captureStream();
        }
      } catch (error) {
        logger.warn('Failed to capture remote audio stream', error);
        speakerStream = null;
      }

      if (speakerStream) {
        speakerSource = speakerAudioCtx.createMediaStreamSource(speakerStream);
      } else {
        speakerSource = speakerAudioCtx.createMediaElementSource(remoteAudio);
      }
    }

    if (!speakerAnalyser) {
      speakerAnalyser = speakerAudioCtx.createAnalyser();
      speakerAnalyser.fftSize = 1024;
      speakerAnalyser.smoothingTimeConstant = 0.7;
      speakerSource.connect(speakerAnalyser);
      speakerDataArray = new Uint8Array(speakerAnalyser.fftSize);
    }

    if (speakerRafId == null) {
      speakerSilenceFrames = 0;
      monitorSpeakerLevel();
    }
  } catch (error) {
    speakerMonitorUnavailable = true;
    logger.warn('Speaker visualizer unavailable', error);
  }
}

function stopSpeakerMonitor(): void {
  if (speakerRafId != null) {
    cancelAnimationFrame(speakerRafId);
    speakerRafId = null;
  }
  speakerSilenceFrames = 0;
  speakerLevel = 0;

  if (!hasAudioState(AudioState.MIC_ACTIVE)) {
    setAssistantSpeakingState(false);
  } else {
    updateAudioVisualization();
  }
}

function monitorSpeakerLevel(): void {
  if (!speakerAnalyser || !speakerDataArray) {
    speakerRafId = null;
    return;
  }

  speakerAnalyser.getByteTimeDomainData(speakerDataArray as Uint8Array<ArrayBuffer>);

  let sumSquares = 0;
  for (let i = 0; i < speakerDataArray.length; i++) {
    const centered = (speakerDataArray[i] - 128) / 128;
    sumSquares += centered * centered;
  }

  const rms = Math.sqrt(sumSquares / speakerDataArray.length);
  const level = Math.min(1, rms * 2.8);
  handleSpeakerLevel(level);

  if (level > 0.035) {
    speakerSilenceFrames = 0;
    if (!hasAudioState(AudioState.ASSISTANT_SPEAKING)) {
      setAssistantSpeakingState(true);
    }
  } else if (speakerSilenceFrames < 24) {
    speakerSilenceFrames += 1;
    if (speakerSilenceFrames === 24 && hasAudioState(AudioState.ASSISTANT_SPEAKING)) {
      setAssistantSpeakingState(false);
    }
  }

  speakerRafId = requestAnimationFrame(monitorSpeakerLevel);
}

if (remoteAudio) {
  const handleSpeakerStart = () => { void startSpeakerMonitor(); };
  const handleSpeakerStop = () => { stopSpeakerMonitor(); };

  remoteAudio.addEventListener('play', handleSpeakerStart);
  remoteAudio.addEventListener('playing', handleSpeakerStart);
  remoteAudio.addEventListener('loadeddata', handleSpeakerStart);
  remoteAudio.addEventListener('pause', handleSpeakerStop);
  remoteAudio.addEventListener('ended', handleSpeakerStop);
  remoteAudio.addEventListener('emptied', handleSpeakerStop);
  remoteAudio.addEventListener('suspend', handleSpeakerStop);
  remoteAudio.addEventListener('stalled', handleSpeakerStop);
}

async function setListeningMode(active: boolean) {
  if (!active) {
    micLevel = 0;
  }

  setAudioStateFlag(AudioState.MIC_ACTIVE, active);

  if (active) {
    document.body.classList.add('listening-mode');
    try {
      await radialViz?.start();
    } catch (error) {
      logger.warn('Failed to start radial visualizer', error);
    }
  } else {
    document.body.classList.remove('listening-mode');
    radialViz?.stop();
    updateAudioVisualization();
  }
}

function setInputAudio(enabled: boolean) {
  if (session && (session as any).inputAudioEnabled !== undefined) {
    (session as any).inputAudioEnabled = enabled;
  }
}

async function setMicState(active: boolean) {
  // Note: ARIA attributes are managed by setVoiceButtonState() state machine
  // This function only handles visual feedback and audio input control
  if (active) {
    pttBtn.classList.add('listening');
  } else {
    pttBtn.classList.remove('listening');
  }
  setMicIcon(active);
  await setListeningMode(active);
  setInputAudio(active);
}

// Remove PWA service worker registration; proactively unregister any past SWs
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.getRegistrations()
    .then(regs => regs.forEach(r => r.unregister()))
    .catch(() => {});
}

// Utility functions: delegate status messages to ConversationRenderer
function setTranscript(msg: string, muted = false): void {
  if (conversationController.isStreaming()) return; // Don't override streaming content
  if (conversationRenderer) conversationRenderer.setStatus(msg, muted);
  statusActive = true;
}

function clearStatusIfActive(): void {
  if (!statusActive) return;
  if (conversationRenderer) conversationRenderer.clearStatus();
  statusActive = false;
}

function updateSidebarToggleState(isOpen: boolean): void {
  sidebarToggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
  sidebarToggle.setAttribute('aria-label', isOpen ? 'Close conversation sidebar' : 'Open conversation sidebar');
}

// Show a banner only when there are no turns rendered
function ensureReadyBanner(message: string): void {
  const hasTurns = conversationRenderer?.hasTurns() ?? false;
  if (!hasTurns) {
    setTranscript(message, true);
  }
}

// Ensure a pending "You" bubble exists using ConversationRenderer
let pendingUserMessageId: string | null = null;

function ensurePendingUserBubble(): void {
  if (!conversationRenderer) return;

  clearStatusIfActive();

  if (!pendingUserMessageId) {
    pendingUserMessageId = `pending-user-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    // Reset pending text for a fresh placeholder
    pendingUserText = '';

    conversationRenderer.addMessage({
      id: pendingUserMessageId,
      role: 'user',
      content: 'Listening…',
      timestamp: new Date()
    });
  }
}

// Update the pending bubble with partial transcript
function updatePendingUserPlaceholder(delta: string): void {
  if (!conversationRenderer) return;

  ensurePendingUserBubble();
  pendingUserText += String(delta);

  if (pendingUserMessageId) {
    conversationRenderer.updateMessage(pendingUserMessageId, {
      content: pendingUserText.trim()
    });
  }
}

async function getSessionToken(): Promise<string> {
  console.log('📡 Fetching session token from:', `${CONFIG.API_BASE}/session`);

  try {
    const r = await fetch(`${CONFIG.API_BASE}/session`);
    const contentType = r.headers.get('content-type') || '';
    console.log('📡 Response:', {
      status: r.status,
      contentType,
      url: r.url
    });

    if (!r.ok) {
      const errorText = await r.text();
      console.error('❌ Session request failed:', r.status, errorText);
      if (r.status === 0 || !r.status) {
        throw new Error('Cannot connect to voice server - is it running?');
      }
      throw new Error(`Failed to get session token: ${r.status} ${errorText}`);
    }

    // Check if response is actually JSON before parsing
    if (!contentType.includes('application/json')) {
      const responseText = await r.text();
      console.error('❌ Expected JSON but got:', contentType);
      console.error('📄 Response body (first 500 chars):', responseText.substring(0, 500));
      throw new Error(
        `Server returned ${contentType} instead of JSON. ` +
        `This usually means:\n` +
        `1. Backend server isn't running (check port 8787)\n` +
        `2. Vite proxy misconfigured\n` +
        `3. Wrong API endpoint\n` +
        `Response preview: ${responseText.substring(0, 100)}...`
      );
    }

    const js = await r.json();
    console.log('📡 Session response:', js);

    const token = js.value || js.client_secret?.value;
    if (!token) {
      console.error('❌ No token found in response:', js);
      throw new Error('Invalid session response format - missing token value');
    }

    console.log('✅ Session token obtained');
    return token;
  } catch (error) {
    if (error instanceof TypeError && error.message.includes('fetch')) {
      console.error('❌ Network error - server unreachable');
      throw new Error('Cannot connect to voice server - check if server is running on port 8787');
    }
    if (error instanceof SyntaxError && error.message.includes('JSON')) {
      console.error('❌ JSON parse error - server returned invalid JSON');
      throw new Error('Server returned invalid JSON - check backend logs for errors');
    }
    throw error;
  }
}

// Context-specific tool creation
function createContextTools(config: VoiceAgentConfig): any[] {
  const tools: any[] = [];
  
  for (const toolConfig of config.tools) {
    if (!toolConfig.enabled) continue;
    
    let tool = null;
    if (toolConfig.mcpServer && toolConfig.mcpFunction) {
      // MCP tools (can be in any context)
      tool = createMCPTool(toolConfig);
    } else if (toolConfig.ragDatabase && toolConfig.ragCollection) {
      // RAG tools (work context specific)  
      tool = createRAGTool(toolConfig);
    }
    
    if (tool) {
      tools.push(tool);
    }
  }
  
  return tools;
}

function createMCPTool(toolConfig: any): any {
  if (toolConfig.name === 'get_current_location') {
    return locationTool;
  } else if (toolConfig.name === 'get_whoop_data') {
    return whoopTool;
  }
  // Add more MCP tools as needed
  return null;
}

function createRAGTool(toolConfig: any): any {
  const baseExecute = async ({ query, category }: { query: string, category?: string }) => {
    console.log(`🔍 ${toolConfig.name}:`, query, category);
    try {
      if (!sessionManager) {
        return 'RAG search not available - session not initialized';
      }
      
      const searchOptions: any = { limit: 3 };
      if (category && category !== 'any') {
        searchOptions.type = category as 'financial' | 'product' | 'policy' | 'organizational' | 'strategic';
      }
      
      const results = await sessionManager.searchDocuments(query, searchOptions);
      
      if (results.length === 0) {
        return `No company information found for "${query}"`;
      }
      
      let response = `Found ${results.length} relevant company documents:\n\n`;
      results.forEach((result: any, i: number) => {
        const doc = result.document;
        response += `${i + 1}. **${doc.metadata.type.toUpperCase()}** (relevance: ${(result.score * 100).toFixed(1)}%)\n`;
        response += `   ${doc.content}\n`;
        response += `   Source: ${doc.metadata.source}\n\n`;
      });
      
      return response;
    } catch (error) {
      console.error(`${toolConfig.name} failed:`, error);
      return `Search failed: ${error instanceof Error ? error.message : 'Unknown error'}`;
    }
  };

  // Create tool based on config name
  if (toolConfig.name === 'search_company_knowledge') {
    return tool({
      name: 'search_company_knowledge',
      description: 'Search company documentation, policies, and business data',
      parameters: z.object({
        query: z.string().describe('Search query for company information'),
        category: z.string().describe('Category to filter by: any, financial, product, policy, organizational, strategic').default('any')
      }),
      execute: baseExecute
    });
  } else if (toolConfig.name === 'get_financial_data') {
    return tool({
      name: 'get_financial_data',
      description: 'Access financial reports and business metrics',
      parameters: z.object({
        query: z.string().describe('Query for financial data (revenue, profits, Q3 results, etc.)'),
        category: z.string().describe('Category: any, financial').default('financial')
      }),
      execute: baseExecute
    });
  } else if (toolConfig.name === 'search_team_info') {
    return tool({
      name: 'search_team_info', 
      description: 'Find team member information and organizational data',
      parameters: z.object({
        query: z.string().describe('Query for team/organizational info'),
        category: z.string().describe('Category: any, organizational').default('organizational')
      }),
      execute: baseExecute
    });
  }
  
  console.warn(`Unknown RAG tool: ${toolConfig.name}`);
  return null;
}

// MCP Tool Definitions using Agents SDK tool() function
const locationTool = tool({
  name: 'get_current_location', 
  description: 'Get current GPS location with coordinates and address. Call this whenever the user asks about their location.',
  parameters: z.object({}),
  async execute() {
    console.log('📍 Calling location tool');
    try {
      const response = await fetch(`${CONFIG.API_BASE}/tool`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
          name: 'location.get_current', 
          args: { include_address: true } 
        })
      });
      
      if (!response.ok) {
        throw new Error(`Location API failed: ${response.status}`);
      }
      
      const data = await response.json();
      console.log('📍 Location data received:', data);
      
      // Format the response for the voice agent
      if (data.error) {
        return `Location error: ${data.error}`;
      }
      
      const loc = Array.isArray(data) ? data[0] : data;
      if (!loc) {
        return "No location data available";
      }
      
      let result = `Current location: ${loc.lat?.toFixed(4)}, ${loc.lon?.toFixed(4)}`;
      if (loc.address) {
        result += ` (${loc.address})`;
      }
      if (loc.accuracy) {
        result += `, accuracy: ${loc.accuracy}m`;
      }
      if (loc.timestamp) {
        const time = new Date(loc.timestamp).toLocaleTimeString();
        result += `, updated: ${time}`;
      }
      
      return result;
    } catch (error) {
      console.error('Location tool error:', error);
      return `Failed to get location: ${error instanceof Error ? error.message : 'Unknown error'}`;
    }
  }
});

const whoopTool = tool({
  name: 'get_whoop_recovery',
  description: 'Get current WHOOP recovery score and health data',
  parameters: z.object({
    date: z.string().describe('Date in YYYY-MM-DD format, defaults to today').optional().nullable()
  }),
  async execute({ date }) {
    console.log('💪 Calling WHOOP tool with date:', date);
    try {
      const response = await fetch(`${CONFIG.API_BASE}/tool`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: 'whoop.get_daily',
          args: { date }
        })
      });

      if (!response.ok) {
        throw new Error(`WHOOP API failed: ${response.status}`);
      }

      const data = await response.json();
      console.log('💪 WHOOP data received:', data);

      // Format the response based on the actual WHOOP MCP data structure
      let result = 'Your WHOOP data:\n';
      if (data.recovery_score) {
        result += `Recovery Score: ${data.recovery_score}%\n`;
      }
      if (data.strain) {
        result += `Strain: ${data.strain}\n`;
      }
      if (data.heart_rate) {
        result += `Heart Rate: ${data.heart_rate} bpm\n`;
      }
      if (data.hrv) {
        result += `HRV: ${data.hrv}ms\n`;
      }
      if (data.rhr) {
        result += `Resting HR: ${data.rhr} bpm\n`;
      }
      if (data.sleep_duration) {
        result += `Sleep: ${data.sleep_duration} hours\n`;
      }
      if (data.date) {
        result += `Date: ${data.date}`;
      }

      return result;
    } catch (error) {
      console.error('WHOOP tool error:', error);
      return `Sorry, couldn't get your WHOOP data: ${error instanceof Error ? error.message : 'Unknown error'}`;
    }
  }
});

// Enhanced streaming UI functions - now delegate to ConversationController

function startStreamingResponse(): void {
  logger.debug('Starting streaming response');

  // Update button state to show assistant is responding
  setVoiceButtonState(VoiceButtonState.RESPONDING);

  // Clear status if currently showing (e.g., "Connecting…")
  clearStatusIfActive();

  // Start streaming via controller
  conversationController.startStreaming();
}

function updateStreamingText(): void {
  // No-op: streaming updates happen via appendStreaming in handleStreamingDelta
}

function finalizeStreamingResponse(): void {
  conversationController.finalizeStreaming();

  // Reset button state after response complete
  setVoiceButtonState(VoiceButtonState.READY);
}


function addToolCallToUI(toolName: string, result: any): void {
  if (!conversationRenderer) return;

  const messageId = `tool-${Date.now()}-${Math.random().toString(36).slice(2)}`;

  conversationRenderer.addMessage({
    id: messageId,
    role: 'assistant',
    content: `🔧 Tool: ${toolName}\n${result}`,
    timestamp: new Date()
  });
}


// Enhanced UI functions - now delegate to ConversationController
function addUserTurnToUI(transcript: string, timestamp?: Date): void {
  clearStatusIfActive();
  conversationController.addUserTurn(transcript, timestamp);
}

function addAssistantTurnToUI(response: string, timestamp?: Date): void {
  conversationController.addAssistantTurn(response, timestamp);
}

// Main connection function using Agents SDK
async function connect(): Promise<void> {
  if (isConnecting()) return; // Prevent concurrent connections
  console.log('🔗 Connect starting...');

  let loadingOverlay: HTMLDivElement | null = null;
  try {
    setVoiceButtonState(VoiceButtonState.CONNECTING);
    loadingOverlay = uiEnhancements.showLoading('Connecting to voice service...');

    // Use the context-aware agent created during initialization
    if (!agent) {
      console.error('❌ Agent not initialized');
      throw new Error('Agent not initialized - context system may have failed');
    }

    console.log('✅ Agent found:', agent.name || 'unnamed agent');

    // Request mic only if needed immediately (e.g. hands-free was already on)
    // Otherwise let PTT/Hands-Free toggle handle it
    if (voiceController.isHandsFreeEnabled()) {
       if (!sharedMicStream) {
          sharedMicStream = await voiceController.requestMicrophone();
       }
       radialViz?.provideStream(sharedMicStream);
    }

    // Use sessionHandler to create and connect session
    const tools = currentContext ? createContextTools(currentContext) : [];
    const { session: newSession, agent: sessionAgent } = await sessionHandler.connect({
      context: currentContext!,
      mediaStream: sharedMicStream || undefined,
      audioElement: remoteAudio || undefined,
      tools,
      onTokenRequest: getSessionToken
    });

    // Update global references with the new agent (ensures fresh tools)
    session = newSession;
    agent = sessionAgent; // Update to the new agent with fresh tools
    stateManager.setSession(newSession);
    stateManager.setAgent(sessionAgent); // Update stateManager with new agent

    // Setup session event handlers directly (no more websocketHandler wrapper)
    session.on('transport_event', async (event: any) => {
      const t = event.type || '';

      // DEBUG: Log ALL event types to debug VAD issue
      if (t.includes('speech') || t.includes('input_audio_buffer')) {
        console.log(`[DEBUG VAD] Event type: "${t}"`, event);
      }

      // Transcript handling
      if (t === 'conversation.item.input_audio_transcription.delta') {
        voiceController.handleTranscript(event.delta || '', false);
      }

      if (t === 'conversation.item.input_audio_transcription.completed') {
        voiceController.handleTranscript(event.transcript || '', true);
      }

      // Assistant message handling
      if (t.startsWith('response.output_audio') || t === 'response.output_text.delta') {
        const delta = event.delta || '';
        if (delta) {
          logger.delta(delta);
          conversationController.appendStreaming(delta);
        }
      }

      // VAD speech detection - FIXED: Check exact event type format
      if (t === 'input_audio_buffer.speech_started') {
        console.log('[DEBUG VAD] ✓ Detected speech_started event!');
        voiceController.handleSpeechStart();
      }

      if (t === 'input_audio_buffer.speech_stopped' || t === 'input_audio_buffer.speech_ended' || t === 'input_audio_buffer.speech_end') {
        console.log('[DEBUG VAD] ✓ Detected speech_stopped event!');
        voiceController.handleSpeechStop();
      }

      // Speaker monitoring
      if (t.startsWith('response.output_audio')) {
        void startSpeakerMonitor();
      }

      // Response handling
      if (t === 'response.done') {
        handleResponseComplete(event);
      }

      // Conversation item handling
      if (t === 'conversation.item.added') {
        conversationController.handleItemAdded(event);
      }

      if (t === 'conversation.item.done') {
        conversationController.handleItemDone(event);
      }
    });

    // Error events
    session.on('error', (error: any) => {
      logger.error('WebSocket event error', error);
      setTranscript(`Voice error: ${error.message}`, true);
      triggerHaptic([100, 50, 100]);
      audioFeedback.playErrorTone();
      setVoiceButtonState(VoiceButtonState.IDLE);
    });

    // History updates for conversation persistence
    session.on('history_updated', async (history: any) => {
      logger.conversation('History updated', history.length);
      // Could sync with our conversation manager here
    });

    // Wire session to controllers
    voiceController.setSession(session);
    textChannelController.setSession(session);
    console.log('🎉 Connection established!');

    logger.success('Connected successfully with Agents SDK');
    // Clear any lingering status text so UI is ready
    clearStatusIfActive();

    // Update button to ready state
    setVoiceButtonState(VoiceButtonState.READY);

    // Ensure we're in voice mode after connection (default behavior)
    if (voiceController.isTextMode()) {
      voiceController.transitionToVoice({
        armed: false,
        handsFree: false
      });
    }

    // Haptic + audio feedback on successful connection
    triggerHaptic(50);
    audioFeedback.playConnectChime();

    // Hide loading and show success
    if (loadingOverlay) uiEnhancements.hideLoading(loadingOverlay);
    uiEnhancements.showToast('Connected successfully', 'success');

  } catch (error) {
    logger.error('Connection failed', error);
    // Hide loading if it was created
    if (loadingOverlay) uiEnhancements.hideLoading(loadingOverlay);
    uiEnhancements.showToast(`Connection failed: ${error instanceof Error ? error.message : 'Unknown error'}`, 'error');

    // Error feedback
    triggerHaptic([100, 50, 100]); // Double vibration for error
    audioFeedback.playErrorTone();

    // Return button to idle state on error
    setVoiceButtonState(VoiceButtonState.IDLE);
  }
}

// Enhanced event handlers (preserve our streaming UI)
function handleResponseComplete(event: any): void {
  logger.debug('Response completed', event);

  if (conversationController.isStreaming()) {
    finalizeStreamingResponse();
  }

  // Return to ready state after assistant finishes responding
  if (stateManager.isResponding()) {
    setVoiceButtonState(VoiceButtonState.READY);
  }
}

async function handleUserTranscript(transcript: any): Promise<void> {
  logger.debug('User transcript received', transcript);
  // If a renderer-based placeholder exists, finalize it; otherwise add new
  const finalText = String(transcript || pendingUserText).trim();

  // First, update the UI with the transcript
  if (pendingUserMessageId && conversationRenderer) {
    conversationRenderer.updateMessage(pendingUserMessageId, {
      content: finalText || '—',
    });
    // Persist finalized user turn (recordTurn is now handled internally by conversationController)
    // But since we're using the pending message approach, we need to persist manually
    if (sessionManager) {
      const turn: ConversationTurn = {
        id: crypto.randomUUID(),
        timestamp: new Date(),
        conversationId: conversationController.getConversationId() || undefined,
        userTranscript: finalText
      };
      await sessionManager.addConversationTurn(turn);
    }
    // Clear pending state
    pendingUserMessageId = null;
    pendingUserText = '';
  } else {
    conversationController.addUserTurn(finalText);
  }

  // Check if this is an agent dispatch command
  const agent = findAgentByIntent(finalText);
  if (agent && cachedAgents.length > 0) {
    console.log(`🎯 Voice command matched agent: ${agent.name}`);

    try {
      const result = await jarvisClient.dispatch({ agent_id: agent.id });
      console.log('✅ Agent dispatched from voice command:', result);

      // Add confirmation to UI
      conversationController.addAssistantTurn(`Started ${agent.name}. Check Task Inbox for results.`);
      uiEnhancements.showToast(`Running ${agent.name}...`, 'success');
    } catch (error: any) {
      console.error('Voice dispatch failed:', error);
      conversationController.addAssistantTurn(`Failed to start ${agent.name}: ${error.message}`);
      uiEnhancements.showToast('Agent dispatch failed', 'error');
    }
  }
}

// Disconnect function
async function disconnect() {
  console.log('🔌 Disconnecting from voice service...');

  await setMicState(false);

  try {
    // Use sessionHandler to disconnect
    await sessionHandler.disconnect();
    session = null; // Clear global reference
    stateManager.setSession(null);
    console.log('✅ Disconnected successfully');
    uiEnhancements.showToast('Disconnected', 'info');
  } catch (error) {
    logger.error('Disconnect error', error);
    console.error('❌ Disconnect error:', error);
  } finally {
    // Return button to idle state
    setVoiceButtonState(VoiceButtonState.IDLE);
    // Stop shared stream for privacy
    if (sharedMicStream) {
      sharedMicStream.getTracks().forEach(t => t.stop());
      sharedMicStream = null;
      radialViz?.provideStream(null);
    }
    stopSpeakerMonitor();
    if (speakerAudioCtx) {
      speakerAudioCtx.close().catch(() => undefined);
      speakerAudioCtx = null;
    }
    speakerAnalyser = null;
    speakerDataArray = null;
    speakerSource = null;
    speakerStream = null;
    speakerRafId = null;
    speakerMonitorUnavailable = false;
    speakerLevel = 0;
    micLevel = 0;
    setAudioStateValue(AudioState.IDLE);
  }
}

// New conversation handler (will be assigned later)
const handleNewConversation = async () => {
  if (!sessionManager || !conversationUI) return;

  try {
    // Disconnect current session if connected
    if (isConnected()) {
      await disconnect();
    }

    if (conversationRenderer) {
      conversationRenderer.clear();
      conversationRenderer.setStatus('Starting new conversation...', true);
    }
    const newConversationId = await sessionManager.createNewConversation();
    conversationController.setConversationId(newConversationId);
    stateManager.setConversationId(newConversationId);
    console.log('📝 Started new conversation:', newConversationId);

    // Update conversation UI
    await conversationUI.addNewConversation(newConversationId);

    // Clear UI state for fresh start
    conversationController.clear();

    // Load empty conversation history (will show "No messages yet")
    await conversationController.loadHistory();
  } catch (error) {
    console.error('Failed to create new conversation:', error);
    setTranscript("Failed to create new conversation.", true);
  }
};

// Clear all conversations handler (will be assigned later)
const handleClearConversations = async () => {
  if (!sessionManager || !conversationUI || !conversationRenderer) return;
  const confirmed = window.confirm('Delete ALL conversations? This cannot be undone. Documents will be preserved.');
  if (!confirmed) return;

  try {
    await sessionManager.clearAllConversations();
    await conversationUI.loadConversations();
    conversationUI.clearConversations();
    conversationController.setConversationId(null);
    stateManager.setConversationId(null);
    conversationController.clear();
    conversationRenderer.setStatus('No conversations yet - start a new one', true);
    logger.success('All conversations cleared');
    uiEnhancements.showToast('All conversations cleared', 'success');
  } catch (error) {
    console.error('Failed to clear conversations:', error);
    setTranscript('Failed to clear conversations', true);
  }
};

// Initialize context-aware application
async function initializeApp(): Promise<void> {
  try {
    console.log('🚀 Initializing Jarvis voice agent...');
    console.log('📊 DOM elements found:', {
      pttBtn: !!pttBtn,
      transcript: !!transcriptEl,
      sidebar: !!sidebar,
      voiceButtonContainer: !!voiceButtonContainer
    });

    // Configure session handler callbacks
    sessionHandler.setConfig({
      onSessionReady: (readySession, readyAgent) => {
        console.log('📱 Session handler: Session ready', {
          agent: readyAgent.name,
          session: !!readySession
        });
      },
      onSessionError: (error) => {
        console.error('❌ Session handler: Session error', error);
        uiEnhancements.showToast('Session error - check console', 'error');
      },
      onSessionEnded: () => {
        console.log('👋 Session handler: Session ended');
      }
    });

    // Load the appropriate context
    const contextName = await contextLoader.autoDetectContext();
    currentContext = await contextLoader.loadContext(contextName);
    stateManager.setContext(currentContext);

    console.log(`✅ Loaded context: ${currentContext.name}`);
    console.log(`📋 Instructions: ${currentContext.instructions.substring(0, 100)}...`);

    // Update UI with context branding
    updateUIForContext(currentContext);
    
    // Initialize session manager with context-specific data loading
    sessionManager = createSessionManagerForContext(currentContext);
    stateManager.setSessionManager(sessionManager);
    conversationController.setSessionManager(sessionManager);
    const initialConversationId = await sessionManager.initializeSession(currentContext, contextName);
    stateManager.setConversationId(initialConversationId);
    conversationController.setConversationId(initialConversationId);

    // Initialize conversation UI
    conversationUI = new ConversationUI();
    conversationUI.setSessionManager(sessionManager);
    await conversationUI.loadConversations();

    if (initialConversationId) {
      console.log(`🔄 Resumed conversation: ${initialConversationId}`);
      conversationUI.setActiveConversation(initialConversationId);
      await conversationController.loadHistory();
    } else {
      console.log(`🆕 No existing conversation - ready for new one`);
      setTranscript(`Ready to start new conversation - tap the microphone`, true);
    }
    
    console.log(`🧠 Session initialized with data loading complete`);
    
    // Create agent with context-specific configuration
    const contextTools = createContextTools(currentContext);
    agent = new RealtimeAgent({
      name: currentContext.name,
      instructions: currentContext.instructions,
      tools: contextTools
    });
    stateManager.setAgent(agent);
    
    console.log(`🔧 Added ${contextTools.length} tools for ${currentContext.name}:`, contextTools.map(t => t.name));

    // Run async initialization for controllers (already created synchronously in DOMContentLoaded)
    console.log('🎛️ Running async controller initialization...');
    await voiceController.initialize();
    await textChannelController.initialize();
    console.log('✅ Controller async initialization complete');

    // Setup UI state - button starts in idle state, ready to connect
    setVoiceButtonState(VoiceButtonState.IDLE);
    newConversationBtn.disabled = false;

    // Discover and auto-detect available contexts before creating selector
    await contextLoader.autoDetectContext();

    // Create context selector in UI
    contextLoader.createContextSelector('context-selector-container');
    
    console.log(`✅ ${currentContext.name} voice agent ready`);
    ensureReadyBanner(`${currentContext.name} ready - tap the microphone to start`);
    
  } catch (error) {
    console.error('❌ Failed to initialize app:', error);
    setTranscript('Initialization failed - using default configuration', true);
    
    // Fallback to basic configuration
    agent = new RealtimeAgent({
      name: 'Voice Agent',
      instructions: 'You are a helpful AI assistant.'
    });
    stateManager.setAgent(agent);
  }
}

function updateUIForContext(config: VoiceAgentConfig): void {
  // Update page title and branding
  document.title = config.branding.title;
  
  const titleEl = document.getElementById('appTitle');
  if (titleEl) {
    titleEl.textContent = config.branding.title;
  }
  
  // Update favicon if specified
  if (config.branding.favicon) {
    const favicon = document.querySelector('link[rel="icon"]') as HTMLLinkElement;
    if (favicon) {
      favicon.href = config.branding.favicon;
    }
  }
  
  // Update default prompts (could add quick action buttons)
  console.log(`💡 Default prompts for ${config.name}:`, config.settings.defaultPrompts);
}

// Conversation switching handler
window.addEventListener('conversationSwitched', async (event: any) => {
  const { conversationId } = event.detail;
  console.log(`🔄 Switching to conversation: ${conversationId}`);

  // Disconnect current session if connected
  if (isConnected()) {
    await disconnect();
  }

  conversationController.setConversationId(conversationId);
  stateManager.setConversationId(conversationId);

  // Clear transcript and show loading via renderer
  if (conversationRenderer) {
    conversationRenderer.clear();
    conversationRenderer.setStatus('Loading conversation...', true);
  }

  // Load conversation history into UI
  await conversationController.loadHistory();

  console.log(`✅ Conversation ${conversationId} loaded and ready`);
});

// Context switching handler
window.addEventListener('contextChanged', async (event: any) => {
  const { contextName, config } = event.detail;
  console.log(`🔄 Context changed to: ${contextName}`);
  
  currentContext = config;
  stateManager.setContext(config);
  updateUIForContext(config);
  
  // End current session and reinitialize with new context
  if (sessionManager) {
    await sessionManager.endSession();
  }

  sessionManager = createSessionManagerForContext(config);
  stateManager.setSessionManager(sessionManager);
  conversationController.setSessionManager(sessionManager);
  const contextConversationId = await sessionManager.initializeSession(config, contextName);
  stateManager.setConversationId(contextConversationId);
  conversationController.setConversationId(contextConversationId);

  if (conversationUI) {
    conversationUI.setSessionManager(sessionManager);
    await conversationUI.loadConversations();

    if (contextConversationId) {
      conversationUI.setActiveConversation(contextConversationId);
      await conversationController.loadHistory();
      console.log(`🔄 Session reinitialized for ${config.name}, resumed conversation: ${contextConversationId}`);
    } else {
      setTranscript(`${config.name} - tap the microphone to start`, true);
      console.log(`🔄 Session reinitialized for ${config.name}, ready for new conversation`);
    }
  }
  
  // Update agent configuration with context-specific tools
  if (agent) {
    const contextTools = createContextTools(config);
    agent = new RealtimeAgent({
      name: config.name,
      instructions: config.instructions,
      tools: contextTools
    });
    stateManager.setAgent(agent);
    console.log(`🔧 Updated tools for ${config.name}:`, contextTools.map(t => t.name));
  }
  
  ensureReadyBanner(`${config.name} - tap the microphone to start`);
});

// Initialize page
document.addEventListener("DOMContentLoaded", () => {
  // Initialize DOM element references - must happen INSIDE DOMContentLoaded
  transcriptEl = document.getElementById("transcript") as HTMLDivElement;
  pttBtn = document.getElementById("pttBtn") as HTMLButtonElement;
  voiceButtonContainer = pttBtn; // The voice button itself is the container for visual states
  newConversationBtn = document.getElementById("newConversationBtn") as HTMLButtonElement;
  clearConvosBtn = document.getElementById("clearConvosBtn") as HTMLButtonElement;
  syncNowBtn = document.getElementById("syncNowBtn") as HTMLButtonElement;
  sidebarToggle = document.getElementById("sidebarToggle") as HTMLButtonElement;
  sidebar = document.getElementById("sidebar") as HTMLDivElement;
  voiceStatusText = document.querySelector('.voice-status-text') as HTMLSpanElement;
  handsFreeToggle = document.getElementById('handsFreeToggle') as HTMLButtonElement;
  const remoteAudio = document.getElementById('remoteAudio') as HTMLAudioElement | null;
  const textInput = document.getElementById('textInput') as HTMLInputElement;
  const sendTextBtn = document.getElementById('sendTextBtn');
  const taskInboxContainer = document.getElementById('task-inbox-container');

  // Initialize conversation renderer AFTER DOM elements exist
  conversationRenderer = new ConversationRenderer(transcriptEl);
  conversationController.setRenderer(conversationRenderer);

  // --- HELPER FUNCTIONS ---
  
  function checkMobile() {
    if (window.innerWidth <= 768) {
      if (sidebarToggle) sidebarToggle.style.display = 'flex';
      if (sidebar) updateSidebarToggleState(sidebar.classList.contains('open'));
    } else {
      if (sidebarToggle) sidebarToggle.style.display = 'none';
      if (sidebar) {
        sidebar.classList.remove('open');
        updateSidebarToggleState(false);
      }
    }
  }

  // Initial check
  checkMobile();
  window.addEventListener('resize', checkMobile);

  // Initialize conversation renderer
  conversationRenderer = new ConversationRenderer(transcriptEl);
  conversationController.setRenderer(conversationRenderer);

  // Initialize Audio Visualizer
  radialViz = pttBtn
    ? new RadialVisualizer(pttBtn, { onLevel: handleMicLevel })
    : null;
  
  // Set initial button state
  setVoiceButtonState(VoiceButtonState.IDLE);

  // Initialize controllers BEFORE setting up event handlers
  console.log('🎛️ Initializing interaction controllers (sync)...');

  // Configure voiceController
  voiceController = new VoiceController({
    onStateChange: (state: VoiceState) => {
      // Update UI based on voice state
      if (state.pttActive) {
        stateManager.setVoiceButtonState(VoiceButtonState.SPEAKING);
      } else if (state.armed) {
        stateManager.setVoiceButtonState(VoiceButtonState.READY);
      } else {
        stateManager.setVoiceButtonState(VoiceButtonState.IDLE);
      }

      // Handle audio feedback
      if (state.vadActive) {
        audioFeedback.playVoiceTick();
        setListeningMode(true).catch(() => {});
        ensurePendingUserBubble();
      } else {
        setListeningMode(false).catch(() => {});
      }

      // Handle microphone state
      if (state.active) {
        setMicState(true).catch(() => {});
        ensurePendingUserBubble();
      } else {
        setMicState(false).catch(() => {});
      }
    },
    onArmed: () => {
      setVoiceButtonState(VoiceButtonState.READY);
      logger.debug('Voice armed via callback');
    },
    onMuted: () => {
      setVoiceButtonState(VoiceButtonState.READY);
      logger.debug('Voice muted via callback');
    },
    onTranscript: (text: string, isFinal: boolean) => {
      if (!isFinal) {
        updatePendingUserPlaceholder(text);
      }
      logger.debug(`Transcript callback (final=${isFinal}):`, text.substring(0, 50));
    },
    onVADStateChange: (active: boolean) => {
      console.log(`[DEBUG main.ts] ✓ onVADStateChange callback received: active=${active}`);
      if (active) {
        audioFeedback.playVoiceTick();
        setListeningMode(true).catch(() => {});
        ensurePendingUserBubble();
      } else {
        setListeningMode(false).catch(() => {});
      }
    },
    onModeTransition: (from: 'voice' | 'text', to: 'voice' | 'text') => {
      logger.info(`Mode transition via callback: ${from} → ${to}`);
      if (to === 'text') {
        setVoiceButtonState(VoiceButtonState.READY);
      }
    },
    onFinalTranscript: (text: string) => {
      handleUserTranscript(text);
    },
    onError: (error: Error) => {
      logger.error('Voice controller error:', error);
      setTranscript(`Voice error: ${error.message}`, true);
    }
  });

  // Create textChannelController
  textChannelController = new TextChannelController({
    autoConnect: true,
    maxRetries: 3
  });

  textChannelController.setVoiceController(voiceController);
  textChannelController.setConnectCallback(connect);

  console.log('✅ Interaction controllers created');

  // --- EVENT HANDLERS ---

  // Microphone Button (PTT)
  if (pttBtn) {
    // 1. Click: Connect if IDLE. If Connected, it's handled by PTT logic (no-op for click)
    pttBtn.addEventListener('click', async (e) => {
      // Resume AudioContext from user gesture
      audioFeedback.resumeContext().catch(() => {});

      if (stateManager.isIdle()) {
        await connect();
      }
    });

    // 2. Mouse/Touch PTT (Hold-to-Talk)
    const startTalking = () => {
      if (isConnected() && canStartPTT() && !voiceController.getState().handsFree) {
        voiceController.startPTT();
      }
    };

    const stopTalking = () => {
      if (isConnected() && voiceController.getState().pttActive) {
        voiceController.stopPTT();
      }
    };

    pttBtn.addEventListener('mousedown', startTalking);
    pttBtn.addEventListener('touchstart', (e) => {
      e.preventDefault(); // Prevent mouse emulation
      startTalking();
    });

    pttBtn.addEventListener('mouseup', stopTalking);
    pttBtn.addEventListener('mouseleave', stopTalking);
    pttBtn.addEventListener('touchend', stopTalking);
    pttBtn.addEventListener('touchcancel', stopTalking);


    // 3. Keyboard PTT (Space/Enter) - Hold-to-Talk
    voiceController.setupKeyboardHandlers(pttBtn);

    pttBtn.onkeydown = async (e: KeyboardEvent) => {
      if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        audioFeedback.resumeContext().catch(() => {});

        if (stateManager.isIdle()) {
          await connect();
          return;
        }

        if (canStartPTT() && stateManager.isReady()) {
          voiceController.startPTT();
        }
      }
    };

    pttBtn.onkeyup = (e: KeyboardEvent) => {
      if (e.key === ' ' || e.key === 'Enter') {
        voiceController.stopPTT();
      }
    };

    console.log('✅ Microphone button handlers attached (Hold-to-Talk)');
  } else {
    console.error('❌ Microphone button (pttBtn) not found');
  }

  // Hands-Free Toggle
  if (handsFreeToggle) {
    handsFreeToggle.addEventListener('click', () => {
      const wasEnabled = handsFreeToggle.getAttribute('aria-checked') === 'true';
      const enabled = !wasEnabled;

      handsFreeToggle.setAttribute('aria-checked', enabled.toString());
      console.log(`🎙️ Hands-free mode: ${enabled ? 'enabled' : 'disabled'}`);

      if (enabled && voiceController.isTextMode()) {
        voiceController.transitionToVoice({
          armed: false,
          handsFree: false
        });
      }

      voiceController.setHandsFree(enabled);

      uiEnhancements.showToast(
        enabled ? 'Hands-free mode enabled' : 'Hands-free mode disabled',
        'info'
      );

      if (enabled && isConnected()) {
        setStatusLabel('Voice listening continuously');
      } else if (!enabled && isConnected()) {
        clearStatusLabel();
      }
    });
  }

  // Text Input
  if (textInput && sendTextBtn) {
    const handleTextCommand = async () => {
      const text = textInput.value.trim();
      if (!text) return;

      const agent = findAgentByIntent(text);

      if (agent && jarvisClient) {
        textInput.value = '';
        try {
          const result = await jarvisClient.dispatch({ agent_id: agent.id });
          uiEnhancements.showToast(`Running ${agent.name}...`, 'success');
          conversationController.addUserTurn(text);
          conversationController.addAssistantTurn(`Started ${agent.name}. Check Task Inbox for updates.`);
        } catch (error: any) {
          uiEnhancements.showToast(error.message || 'Dispatch failed', 'error');
          conversationController.addAssistantTurn(`Failed to start ${agent.name}: ${error.message}`);
        }
      } else {
        textInput.value = '';
        conversationController.addUserTurn(text);
        try {
          await textChannelController.sendText(text);
          uiEnhancements.showToast('Message sent', 'success');
        } catch (error: any) {
          uiEnhancements.showToast(`Failed to send message: ${error.message}`, 'error');
        }
      }
    };

    sendTextBtn.addEventListener('click', handleTextCommand);
    textInput.addEventListener('keypress', (e) => {
      if (e.key === 'Enter') {
        handleTextCommand();
      }
    });
  }

  // Sidebar & Other UI
  if (sidebarToggle) {
    sidebarToggle.onclick = () => {
      const isOpen = sidebar.classList.toggle('open');
      updateSidebarToggleState(isOpen);
    };
  }

  // Close sidebar on mobile when clicking outside
  document.addEventListener('click', (e) => {
    if (window.innerWidth <= 768 &&
        sidebar && sidebarToggle &&
        !sidebar.contains(e.target as Node) &&
        e.target !== sidebarToggle) {
      sidebar.classList.remove('open');
      updateSidebarToggleState(false);
    }
  });

  if (syncNowBtn) {
    syncNowBtn.onclick = async () => {
      if (!sessionManager) return;
      syncNowBtn.disabled = true;
      try {
        const { pushed, pulled } = await sessionManager.syncNow();
        uiEnhancements.showToast(`Synced ${pushed + pulled} items`, 'success');
      } catch (e) {
        uiEnhancements.showToast('Sync failed', 'error');
      } finally {
        syncNowBtn.disabled = false;
      }
    };
  }

  newConversationBtn.onclick = handleNewConversation;
  clearConvosBtn.onclick = handleClearConversations;

  // Speaker Monitor for Remote Audio
  if (remoteAudio) {
    const handleSpeakerStart = () => { void startSpeakerMonitor(); };
    const handleSpeakerStop = () => { stopSpeakerMonitor(); };

    remoteAudio.addEventListener('play', handleSpeakerStart);
    remoteAudio.addEventListener('playing', handleSpeakerStart);
    remoteAudio.addEventListener('loadeddata', handleSpeakerStart);
    remoteAudio.addEventListener('pause', handleSpeakerStop);
    remoteAudio.addEventListener('ended', handleSpeakerStop);
    remoteAudio.addEventListener('emptied', handleSpeakerStop);
    remoteAudio.addEventListener('suspend', handleSpeakerStop);
    remoteAudio.addEventListener('stalled', handleSpeakerStop);
  }
  
  // Initialize App Logic
  initializeApp();

  const params = new URLSearchParams(window.location.search);
  const shouldAuto = (params.get('autoconnect') === '1') || (localStorage.getItem('jarvis.autoconnect') === '1');
  if (shouldAuto) {
    setTimeout(() => connect(), 0);
  }
});

// Expose functions for testing
declare global {
  interface Window {
    addUserTurnToUI: typeof addUserTurnToUI;
    addAssistantTurnToUI: typeof addAssistantTurnToUI;
    debugConversations: () => Promise<void>;
    syncNow: () => Promise<{ pushed: number; pulled: number }>;
    flushLocal: () => Promise<void>;
    ConversationRenderer: typeof ConversationRenderer;
    conversationController: typeof conversationController;
    // Feedback preferences (Phase 6)
    getFeedbackPreferences: () => FeedbackPreferences;
    setHapticFeedback: (enabled: boolean) => void;
    setAudioFeedback: (enabled: boolean) => void;
    testAudioFeedback: () => void;
  }
}

window.addUserTurnToUI = addUserTurnToUI;
window.addAssistantTurnToUI = addAssistantTurnToUI;
window.ConversationRenderer = ConversationRenderer;
window.conversationController = conversationController;
window.syncNow = async () => {
  if (!sessionManager) return { pushed: 0, pulled: 0 };
  return await sessionManager.syncNow();
};
window.flushLocal = async () => {
  if (!sessionManager) return;
  await sessionManager.flush();
};

// Feedback preference controls (Phase 6)
window.getFeedbackPreferences = () => {
  return { ...feedbackPrefs };
};

window.setHapticFeedback = (enabled: boolean) => {
  feedbackPrefs.haptics = enabled;
  saveFeedbackPreferences(feedbackPrefs);
  console.log(`Haptic feedback ${enabled ? 'enabled' : 'disabled'}`);
};

window.setAudioFeedback = (enabled: boolean) => {
  feedbackPrefs.audio = enabled;
  audioFeedback.setAudioEnabled(enabled);
  saveFeedbackPreferences(feedbackPrefs);
  console.log(`Audio feedback ${enabled ? 'enabled' : 'disabled'}`);
};

window.testAudioFeedback = () => {
  console.log('Testing audio feedback...');
  audioFeedback.playConnectChime();
  setTimeout(() => audioFeedback.playVoiceTick(), 500);
  setTimeout(() => audioFeedback.playErrorTone(), 1000);
  console.log('Played: connect chime → voice tick → error tone');
};

// Debug function to check IndexedDB contents
window.debugConversations = async () => {
  if (!sessionManager) {
    console.log('❌ No session manager');
    return;
  }

  const conversations = await sessionManager.getAllConversations();
  console.log('📋 All conversations:', conversations);

  const currentId = conversationController.getConversationId();
  if (currentId) {
    const history = await sessionManager.getConversationHistory();
    console.log(`💬 Current conversation (${currentId}) history:`, history);
  }
};

// Initialize Jarvis-Zerg integration
async function initializeJarvisIntegration() {
  try {
    const zergApiURL = import.meta.env?.VITE_ZERG_API_URL || 'http://localhost:47300';
    const deviceSecret = import.meta.env?.VITE_JARVIS_DEVICE_SECRET;

    if (!deviceSecret) {
      console.warn('⚠️ VITE_JARVIS_DEVICE_SECRET not configured - Zerg integration disabled');
      return;
    }

    console.log('🔌 Initializing Jarvis-Zerg integration...');

    // Authenticate with Zerg
    if (!jarvisClient.isAuthenticated()) {
      await jarvisClient.authenticate(deviceSecret);
      console.log('✅ Authenticated with Zerg backend');
    }

    // Load available agents for voice/text commands
    cachedAgents = await jarvisClient.listAgents();
    console.log(`✅ Loaded ${cachedAgents.length} agents from Zerg`);

    // Initialize Task Inbox
    const taskInboxContainer = document.getElementById('task-inbox-container');
    if (taskInboxContainer) {
      taskInbox = await createTaskInbox(taskInboxContainer, {
        apiURL: zergApiURL,
        deviceSecret: deviceSecret,
        onError: (error) => {
          console.error('Task Inbox error:', error);
          uiEnhancements.showToast('Task Inbox error - check console', 'error');
        },
        onRunUpdate: (run) => {
          // Speak result when agent completes successfully
          if (run.status === 'success' && run.summary) {
            console.log(`✅ Agent "${run.agent_name}" completed:`, run.summary);
            // Optional: Add TTS here when ready
            // const utterance = new SpeechSynthesisUtterance(run.summary);
            // speechSynthesis.speak(utterance);
          }
        },
      });
      console.log('✅ Task Inbox initialized');

      // Show Task Inbox on wide screens
      if (window.innerWidth >= 1440) {
        taskInboxContainer.classList.add('visible');
      }
    }

  } catch (error) {
    console.error('❌ Jarvis-Zerg integration failed:', error);
    // Non-fatal - text input and voice features still work
  }
}

// Map user text/voice to agent intent
function findAgentByIntent(text: string): JarvisAgentSummary | null {
  const lower = text.toLowerCase();

  // Agent keywords mapping
  if (lower.includes('morning') || lower.includes('digest')) {
    return cachedAgents.find(a => a.name === 'Morning Digest') || null;
  }
  if (lower.includes('health') || lower.includes('recovery') || lower.includes('whoop')) {
    return cachedAgents.find(a => a.name === 'Health Watch') || null;
  }
  if (lower.includes('status') || lower.includes('quick check')) {
    return cachedAgents.find(a => a.name === 'Quick Status Check') || null;
  }
  if (lower.includes('planning') || lower.includes('week ahead')) {
    return cachedAgents.find(a => a.name === 'Weekly Planning Assistant') || null;
  }

  // Check if text starts with "run" or "execute"
  if (lower.startsWith('run ') || lower.startsWith('execute ')) {
    // Extract agent name after "run"
    const agentNamePart = lower.replace(/^(run|execute)\s+/, '').trim();
    return cachedAgents.find(a =>
      a.name.toLowerCase().includes(agentNamePart)
    ) || null;
  }

  return null;
}

// Initialize integration after DOM ready
document.addEventListener('DOMContentLoaded', () => {
  initializeJarvisIntegration().catch(err => {
    console.error('Integration initialization failed:', err);
  });
});

console.log('Jarvis PWA with Agents SDK ready', {
  pttBtn: !!pttBtn,
  transcript: !!transcriptEl,
  newConversationBtn: !!newConversationBtn
});
