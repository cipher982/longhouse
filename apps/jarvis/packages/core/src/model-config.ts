/**
 * Centralized OpenAI model configuration for Jarvis
 *
 * Configure via environment variables:
 * - JARVIS_REALTIME_MODEL: Main realtime model (default: gpt-4o-realtime-preview)
 * - JARVIS_REALTIME_MODEL_MINI: Smaller/cheaper model for tests (default: gpt-4o-mini-realtime-preview)
 * - JARVIS_USE_MINI_MODEL: Set to "true" to use mini model (for tests)
 *
 * Available models (as of Dec 2024):
 * - gpt-4o-realtime-preview: Full capability, higher cost
 * - gpt-4o-mini-realtime-preview: Smaller, faster, cheaper - good for tests
 */

// Default models
const DEFAULT_REALTIME_MODEL = 'gpt-4o-realtime-preview';
const DEFAULT_REALTIME_MODEL_MINI = 'gpt-4o-mini-realtime-preview';

// Legacy model name mapping (OpenAI changed naming)
const MODEL_ALIASES: Record<string, string> = {
  'gpt-realtime': 'gpt-4o-realtime-preview',
  'gpt-4-realtime': 'gpt-4o-realtime-preview',
};

/**
 * Get environment variable (works in Node, Bun, and Vite)
 */
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

/**
 * Resolve model name, handling aliases
 */
function resolveModelName(model: string): string {
  return MODEL_ALIASES[model] || model;
}

/**
 * Model configuration object
 */
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
 * Get model configuration from environment
 */
export function getModelConfig(): ModelConfig {
  const realtimeModel = resolveModelName(
    getEnv('JARVIS_REALTIME_MODEL') || DEFAULT_REALTIME_MODEL
  );

  const realtimeModelMini = resolveModelName(
    getEnv('JARVIS_REALTIME_MODEL_MINI') || DEFAULT_REALTIME_MODEL_MINI
  );

  const useMiniModel = getEnv('JARVIS_USE_MINI_MODEL') === 'true';

  const activeModel = useMiniModel ? realtimeModelMini : realtimeModel;

  const defaultVoice = getEnv('JARVIS_VOICE') || 'verse';

  return {
    realtimeModel,
    realtimeModelMini,
    useMiniModel,
    activeModel,
    defaultVoice,
  };
}

/**
 * Get the active realtime model
 * Convenience function for most use cases
 */
export function getRealtimeModel(): string {
  return getModelConfig().activeModel;
}

/**
 * Get the default voice
 */
export function getDefaultVoice(): string {
  return getModelConfig().defaultVoice;
}

// Export defaults for reference
export const MODELS = {
  REALTIME: DEFAULT_REALTIME_MODEL,
  REALTIME_MINI: DEFAULT_REALTIME_MODEL_MINI,
} as const;

// For backward compatibility
export const DEFAULT_MODEL = DEFAULT_REALTIME_MODEL;
