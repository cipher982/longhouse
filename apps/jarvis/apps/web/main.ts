/**
 * Jarvis PWA Main Orchestrator
 * Coordinates all modules for the voice assistant application
 */

import { RealtimeAgent } from '@openai/agents/realtime';
import { tool } from '@openai/agents';
import { z } from 'zod';
import { logger, getJarvisClient } from '@jarvis/core';
import { ConversationUI } from './lib/conversation-ui';
import { ConversationRenderer } from './lib/conversation-renderer';
import { createTaskInbox } from './lib/task-inbox';
import { contextLoader } from './contexts/context-loader';
import { uiEnhancements } from './lib/ui-enhancements';
import { RadialVisualizer } from './lib/radial-visualizer';
import { InteractionStateMachine } from './lib/interaction-state-machine';
import { VoiceChannelController } from './lib/voice-channel-controller';
import { TextChannelController } from './lib/text-channel-controller';

// Import new modules
import { CONFIG, VoiceButtonState, getZergApiUrl } from './lib/config';
import { stateManager } from './lib/state-manager';
import { feedbackSystem } from './lib/feedback-system';
import { sessionHandler } from './lib/session-handler';
import { voiceManager } from './lib/voice-manager';
import { uiController } from './lib/ui-controller';
import { websocketHandler } from './lib/websocket-handler';

// Tool definitions (these would be moved to a separate file in a full refactor)
const tools = {
  // MCP Tool Definitions using Agents SDK tool() function
  getLocation: tool({
    name: 'get_location',
    description: 'Get the current location coordinates',
    parameters: z.object({}),
    execute: async () => {
      try {
        const response = await fetch('/mcp/location/get_location', { method: 'POST' });
        const data = await response.json();
        return {
          latitude: data.latitude || 40.8041,
          longitude: data.longitude || -96.6817,
          accuracy: data.accuracy || 'approximate'
        };
      } catch (error) {
        logger.error('Failed to get location:', error);
        return { latitude: 40.8041, longitude: -96.6817, accuracy: 'fallback' };
      }
    }
  }),

  getWeather: tool({
    name: 'get_weather',
    description: 'Get current weather for a location',
    parameters: z.object({
      latitude: z.number(),
      longitude: z.number()
    }),
    execute: async ({ latitude, longitude }) => {
      try {
        const response = await fetch('/mcp/weather/get_weather', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ latitude, longitude })
        });
        return await response.json();
      } catch (error) {
        logger.error('Failed to get weather:', error);
        throw error;
      }
    }
  }),

  getSolarData: tool({
    name: 'get_solar_data',
    description: 'Get sunrise and sunset times for a location',
    parameters: z.object({
      latitude: z.number(),
      longitude: z.number()
    }),
    execute: async ({ latitude, longitude }) => {
      try {
        const response = await fetch('/mcp/solar/get_solar_data', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ latitude, longitude })
        });
        return await response.json();
      } catch (error) {
        logger.error('Failed to get solar data:', error);
        throw error;
      }
    }
  })
};

/**
 * Initialize the application
 */
async function initialize(): Promise<void> {
  try {
    // Initialize UI controller
    uiController.initialize();

    // Initialize feedback system
    await feedbackSystem.initialize();

    // Load context
    const context = await contextLoader.loadContext('personal');
    if (!context) {
      throw new Error('Failed to load context');
    }

    // Initialize controllers
    const interactionStateMachine = new InteractionStateMachine();
    const voiceChannelController = new VoiceChannelController();
    const textChannelController = new TextChannelController();

    // Store controllers in state
    stateManager.setControllers({
      interactionStateMachine,
      voiceChannelController,
      textChannelController
    });

    // Initialize Jarvis client
    const jarvisClient = getJarvisClient(getZergApiUrl());
    stateManager.setJarvisClient(jarvisClient);

    // Initialize conversation renderer
    const conversationRenderer = new ConversationRenderer('conversation');
    stateManager.setConversationRenderer(conversationRenderer);

    // Initialize visualizer
    const visualizer = new RadialVisualizer('voiceButtonContainer');
    voiceManager.initialize(visualizer);

    // Setup event handlers
    setupEventHandlers();

    // Setup UI enhancements
    uiEnhancements.init();

    logger.info('Application initialized successfully');

  } catch (error) {
    logger.error('Failed to initialize application:', error);
    uiController.showError('Failed to initialize application');
  }
}

/**
 * Setup event handlers
 */
function setupEventHandlers(): void {
  // Get DOM elements
  const voiceButton = uiController.getVoiceButton();
  const connectButton = uiController.getConnectButton();
  const textInput = uiController.getTextInput();
  const sendButton = uiController.getSendButton();
  const handsFreeToggle = uiController.getHandsFreeToggle();

  // Voice button handlers
  if (voiceButton) {
    voiceButton.addEventListener('click', handleVoiceButtonClick);
    voiceManager.setupVoiceButton(voiceButton);
  }

  // Connect button handler
  if (connectButton) {
    connectButton.addEventListener('click', handleConnectButtonClick);
  }

  // Text input handlers
  if (textInput && sendButton) {
    textInput.addEventListener('keypress', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSendText();
      }
    });

    sendButton.addEventListener('click', handleSendText);
  }

  // Hands-free toggle handler
  if (handsFreeToggle) {
    handsFreeToggle.addEventListener('change', () => {
      voiceManager.handleHandsFreeToggle(handsFreeToggle.checked);
    });
  }

  // Keyboard shortcuts
  voiceManager.setupKeyboardShortcuts();

  // Session handler callbacks
  sessionHandler.config = {
    onSessionReady: (session) => {
      websocketHandler.setupSessionHandlers(session);
      uiController.updateStatus('Ready to chat');
    },
    onSessionError: (error) => {
      uiController.showError(error.message);
    },
    onSessionEnded: () => {
      uiController.updateStatus('Session ended');
    }
  };

  // Voice manager callbacks
  voiceManager.config = {
    onTranscript: (text, isFinal) => {
      const renderer = stateManager.getState().conversationRenderer;
      if (renderer) {
        if (isFinal) {
          renderer.finalizePendingUserBubble(text);
        } else {
          renderer.showPendingUserBubble(text);
        }
      }
    },
    onVADStateChange: (active) => {
      logger.debug('VAD state:', active);
    }
  };

  // WebSocket handler callbacks
  websocketHandler.config = {
    onMessage: (message) => {
      logger.debug('WebSocket message:', message);
    },
    onError: (error) => {
      uiController.showError(error.message);
    }
  };
}

/**
 * Handle voice button click
 */
async function handleVoiceButtonClick(): Promise<void> {
  const state = stateManager.getState();

  if (stateManager.isIdle()) {
    // Not connected - initiate connection
    const context = await contextLoader.loadContext('personal');
    if (context) {
      await sessionHandler.connect(context);
    }
  } else if (stateManager.isConnected()) {
    // Connected - this shouldn't happen with PTT
    logger.debug('Voice button clicked while connected');
  }
}

/**
 * Handle connect button click
 */
async function handleConnectButtonClick(): Promise<void> {
  if (stateManager.isConnected()) {
    // Disconnect
    await sessionHandler.disconnect();
  } else if (!stateManager.isConnecting()) {
    // Connect
    const context = await contextLoader.loadContext('personal');
    if (context) {
      await sessionHandler.connect(context);
    }
  }
}

/**
 * Handle send text
 */
async function handleSendText(): Promise<void> {
  const textInput = uiController.getTextInput();
  if (!textInput) return;

  const text = textInput.value.trim();
  if (!text) return;

  const state = stateManager.getState();
  const { textChannelController } = state;

  if (textChannelController) {
    try {
      await textChannelController.sendText(text);
      uiController.clearTextInput();
    } catch (error) {
      logger.error('Failed to send text:', error);
      uiController.showError('Failed to send message');
    }
  }
}

/**
 * Service worker unregistration
 */
async function unregisterServiceWorkers(): Promise<void> {
  if ('serviceWorker' in navigator) {
    const registrations = await navigator.serviceWorker.getRegistrations();
    for (const registration of registrations) {
      await registration.unregister();
      logger.info('Service worker unregistered:', registration.scope);
    }
  }
}

/**
 * Main entry point
 */
document.addEventListener('DOMContentLoaded', async () => {
  logger.info('Jarvis PWA starting...');

  // Unregister any service workers
  await unregisterServiceWorkers();

  // Initialize application
  await initialize();

  // Log ready state
  logger.info('Jarvis PWA ready');
});

/**
 * Cleanup on page unload
 */
window.addEventListener('beforeunload', () => {
  sessionHandler.cleanup();
  feedbackSystem.cleanup();
  voiceManager.cleanup();
  uiController.cleanup();
  websocketHandler.cleanup();
});