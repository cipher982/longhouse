/**
 * Centralized model configuration for Jarvis.
 *
 * Model definitions are synchronized with config/models.json (the source of truth).
 * This file contains hardcoded defaults that match the JSON for reliability across
 * all JavaScript environments (Node, Bun, Vitest, browser).
 *
 * Environment variable overrides:
 * - JARVIS_REALTIME_MODEL: Override the TIER_1 realtime model
 * - JARVIS_REALTIME_MODEL_MINI: Override the TIER_2 realtime model
 * - JARVIS_USE_MINI_MODEL: Set to "true" to use TIER_2 model
 * - JARVIS_VOICE: Override default voice (default: verse)
 */

// =============================================================================
// MODEL CONSTANTS - Kept in sync with config/models.json
// =============================================================================

// Realtime model tiers (Jarvis voice interface)
export const REALTIME_TIER_1 = 'gpt-4o-realtime-preview';
export const REALTIME_TIER_2 = 'gpt-4o-mini-realtime-preview';

// Text model tiers (for reference, primarily used by Zerg Python backend)
export const TEXT_TIER_1 = 'gpt-5.1';
export const TEXT_TIER_2 = 'gpt-5-mini';
export const TEXT_TIER_3 = 'gpt-5-nano';

// Default voice
const DEFAULT_VOICE = 'verse';

// Legacy model name mapping
const MODEL_ALIASES: Record<string, string> = {
  'gpt-realtime': 'gpt-4o-realtime-preview',
  'gpt-4-realtime': 'gpt-4o-realtime-preview',
};

// =============================================================================
// ENVIRONMENT HELPERS
// =============================================================================

function getEnv(key: string): string | undefined {
  // Node/Bun
  if (typeof process !== 'undefined' && process.env) {
    return process.env[key];
  }
  // Vite (import.meta.env)
  if (typeof import.meta !== 'undefined' && (import.meta as any).env) {
    return (import.meta as any).env[`VITE_${key}`] || (import.meta as any).env[key];
  }
  return undefined;
}

function resolveModelName(model: string): string {
  return MODEL_ALIASES[model] || model;
}

// =============================================================================
// PUBLIC API
// =============================================================================

export interface ModelConfig {
  /** Main realtime model */
  realtimeModel: string;
  /** Mini/test realtime model */
  realtimeModelMini: string;
  /** Whether to use mini model */
  useMiniModel: boolean;
  /** Get the active model based on config */
  activeModel: string;
  /** Default voice for realtime */
  defaultVoice: string;
}

/**
 * Get model configuration from environment (with overrides) or defaults.
 */
export function getModelConfig(): ModelConfig {
  const realtimeModel = resolveModelName(getEnv('JARVIS_REALTIME_MODEL') || REALTIME_TIER_1);
  const realtimeModelMini = resolveModelName(getEnv('JARVIS_REALTIME_MODEL_MINI') || REALTIME_TIER_2);
  const useMiniModel = getEnv('JARVIS_USE_MINI_MODEL') === 'true';
  const activeModel = useMiniModel ? realtimeModelMini : realtimeModel;
  const defaultVoice = getEnv('JARVIS_VOICE') || DEFAULT_VOICE;

  return {
    realtimeModel,
    realtimeModelMini,
    useMiniModel,
    activeModel,
    defaultVoice,
  };
}

/**
 * Get the active realtime model.
 * Convenience function for most use cases.
 */
export function getRealtimeModel(): string {
  return getModelConfig().activeModel;
}

/**
 * Get the default voice.
 */
export function getDefaultVoice(): string {
  return getModelConfig().defaultVoice;
}

// =============================================================================
// EXPORTED CONSTANTS (backwards compatible)
// =============================================================================

export const MODELS = {
  REALTIME: REALTIME_TIER_1,
  REALTIME_MINI: REALTIME_TIER_2,
} as const;

// For backward compatibility
export const DEFAULT_MODEL = REALTIME_TIER_1;
export const DEFAULT_REALTIME_MODEL = REALTIME_TIER_1;
export const DEFAULT_REALTIME_MODEL_MINI = REALTIME_TIER_2;
