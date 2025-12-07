/**
 * Configuration module for Jarvis PWA
 * Centralizes all configuration, environment variables, and default settings
 */

import { logger } from '@jarvis/core';
import type { ConversationManagerOptions, SyncTransport } from '@jarvis/data-local';

// Main configuration object
export const CONFIG = {
  // Use relative path for API - proxied through Nginx
  API_BASE: '/api',

  // Default Zerg API URL
  DEFAULT_ZERG_API_URL: 'http://localhost:47300',

  // Voice interaction settings
  VOICE: {
    DEFAULT_MAX_HISTORY_TURNS: 10,
    VAD_TIMEOUT_MS: 3000,
    RECONNECT_DELAY_MS: 1000,
  },

  // UI settings
  UI: {
    STATUS_LABEL_TIMEOUT_MS: 3000,
    ANIMATION_DURATION_MS: 300,
  },

  // Feedback settings
  FEEDBACK: {
    HAPTIC_PATTERNS: {
      CONNECT: [50],
      DISCONNECT: [100, 50, 50],
      START_SPEAKING: [25],
      STOP_SPEAKING: [25, 25],
      ERROR: [200, 100, 200],
    },
    AUDIO_TONES: {
      CONNECT: { frequency: 440, duration: 100 },
      DISCONNECT: { frequency: 220, duration: 150 },
      START_SPEAKING: { frequency: 660, duration: 50 },
      STOP_SPEAKING: { frequency: 440, duration: 50 },
    }
  },

  // Storage keys
  STORAGE_KEYS: {
    FEEDBACK_PREFERENCES: 'jarvis.feedback.preferences',
    LAST_CONTEXT: 'jarvis.last.context',
    SESSION_STATE: 'jarvis.session.state',
  }
};

// Voice Button State Machine states
export enum VoiceButtonState {
  IDLE = 'idle',             // Disconnected/not ready
  CONNECTING = 'connecting', // In the process of connecting
  READY = 'ready',           // Connected and ready for interaction
  SPEAKING = 'speaking',     // User is speaking (PTT or VAD active)
  RESPONDING = 'responding', // AI is responding
  ACTIVE = 'active',         // General active state (listening/processing)
  PROCESSING = 'processing'  // Busy processing (disconnecting/thinking)
}

// Feedback preferences interface
export interface FeedbackPreferences {
  haptics: boolean;
  audio: boolean;
}

/**
 * Resolve a sync base URL from various input formats
 */
export function resolveSyncBaseUrl(raw?: string): string {
  const fallback = CONFIG.API_BASE;
  if (!raw) return fallback;

  const trimmed = raw.trim();
  if (trimmed === '' || trimmed.toLowerCase() === 'auto') {
    return fallback;
  }

  if (/^https?:\/\//i.test(trimmed)) {
    return trimmed.replace(/\/$/, '');
  }

  if (trimmed.startsWith('//')) {
    return `${window.location.protocol}${trimmed}`.replace(/\/$/, '');
  }

  if (/^[\w.-]+:\d+$/.test(trimmed)) {
    return `${window.location.protocol}//${trimmed}`.replace(/\/$/, '');
  }

  if (trimmed.startsWith('/')) {
    return `${window.location.origin}${trimmed}`.replace(/\/$/, '');
  }

  try {
    return new URL(trimmed, window.location.origin).toString().replace(/\/$/, '');
  } catch (error) {
    logger.warn('Invalid sync base URL, using fallback', { provided: trimmed, error });
    return fallback;
  }
}

/**
 * Create a sync transport with optional headers
 */
export function createSyncTransport(headers?: Record<string, string>): SyncTransport {
  return async (input, init = {}) => {
    if (typeof fetch === 'undefined') {
      throw new Error('Fetch is not available for sync transport');
    }

    const mergedHeaders = {
      ...(headers ?? {}),
      ...(init.headers ?? {})
    };

    return fetch(input, { ...init, headers: mergedHeaders });
  };
}

/**
 * Build conversation manager options from context
 */
export function buildConversationManagerOptions(config: any): ConversationManagerOptions {
  const syncBaseUrl = resolveSyncBaseUrl(config?.sync?.baseUrl);
  const syncTransport = createSyncTransport(config?.sync?.headers);

  return {
    syncBaseUrl,
    syncTransport
  };
}

/**
 * Get Zerg API URL - now uses relative path for same-origin calls
 */
export function getZergApiUrl(): string {
  // Use relative path for same-origin API calls through nginx proxy
  return '/api';
}

/**
 * Load feedback preferences from localStorage
 */
export function loadFeedbackPreferences(): FeedbackPreferences {
  try {
    const stored = localStorage.getItem(CONFIG.STORAGE_KEYS.FEEDBACK_PREFERENCES);
    if (stored) {
      return JSON.parse(stored);
    }
  } catch (error) {
    logger.warn('Failed to load feedback preferences', error);
  }
  // Default: enabled
  return { haptics: true, audio: true };
}

/**
 * Save feedback preferences to localStorage
 */
export function saveFeedbackPreferences(prefs: FeedbackPreferences): void {
  try {
    localStorage.setItem(CONFIG.STORAGE_KEYS.FEEDBACK_PREFERENCES, JSON.stringify(prefs));
  } catch (error) {
    logger.warn('Failed to save feedback preferences', error);
  }
}
