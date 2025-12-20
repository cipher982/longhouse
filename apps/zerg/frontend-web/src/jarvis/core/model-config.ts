/**
 * Centralized model configuration for Jarvis.
 *
 * NOTE: These values are inlined from config/models.json to avoid workspace
 * dependency issues in Docker builds. If you update models.json, update here too.
 *
 * Environment variable overrides:
 * - JARVIS_REALTIME_MODEL: Override the TIER_1 realtime model
 * - JARVIS_REALTIME_MODEL_MINI: Override the TIER_2 realtime model
 * - JARVIS_USE_MINI_MODEL: Set to "true" to use TIER_2 model
 * - JARVIS_VOICE: Override default voice
 */

// =============================================================================
// INLINED CONFIG VALUES (from config/models.json)
// =============================================================================

const realtimeConfig = {
  tiers: {
    TIER_1: 'gpt-4o-realtime-preview',
    TIER_2: 'gpt-4o-mini-realtime-preview',
  },
  aliases: {
    'gpt-realtime': 'gpt-4o-realtime-preview',
    'gpt-4-realtime': 'gpt-4o-realtime-preview',
  } as Record<string, string>,
  defaultVoice: 'verse',
};

const textConfig = {
  tiers: {
    TIER_1: 'gpt-5.1',
    TIER_2: 'gpt-5-mini',
    TIER_3: 'gpt-5-nano',
  },
};

// =============================================================================
// EXPORTS
// =============================================================================

// Realtime model tiers (Jarvis voice interface)
export const REALTIME_TIER_1 = realtimeConfig.tiers.TIER_1;
export const REALTIME_TIER_2 = realtimeConfig.tiers.TIER_2;

// Text model tiers (for reference, primarily used by Zerg Python backend)
export const TEXT_TIER_1 = textConfig.tiers.TIER_1;
export const TEXT_TIER_2 = textConfig.tiers.TIER_2;
export const TEXT_TIER_3 = textConfig.tiers.TIER_3;

// =============================================================================
// HELPERS
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
  return realtimeConfig.aliases[model] || model;
}

// =============================================================================
// PUBLIC API
// =============================================================================

export interface ModelConfig {
  realtimeModel: string;
  realtimeModelMini: string;
  useMiniModel: boolean;
  activeModel: string;
  defaultVoice: string;
}

export function getModelConfig(): ModelConfig {
  const realtimeModel = resolveModelName(getEnv('JARVIS_REALTIME_MODEL') || REALTIME_TIER_1);
  const realtimeModelMini = resolveModelName(getEnv('JARVIS_REALTIME_MODEL_MINI') || REALTIME_TIER_2);
  const useMiniModel = getEnv('JARVIS_USE_MINI_MODEL') === 'true';

  return {
    realtimeModel,
    realtimeModelMini,
    useMiniModel,
    activeModel: useMiniModel ? realtimeModelMini : realtimeModel,
    defaultVoice: getEnv('JARVIS_VOICE') || realtimeConfig.defaultVoice,
  };
}

export function getRealtimeModel(): string {
  return getModelConfig().activeModel;
}

export function getDefaultVoice(): string {
  return getModelConfig().defaultVoice;
}

// =============================================================================
// BACKWARDS COMPATIBLE EXPORTS
// =============================================================================

export const MODELS = {
  REALTIME: REALTIME_TIER_1,
  REALTIME_MINI: REALTIME_TIER_2,
} as const;

export const DEFAULT_MODEL = REALTIME_TIER_1;
export const DEFAULT_REALTIME_MODEL = REALTIME_TIER_1;
export const DEFAULT_REALTIME_MODEL_MINI = REALTIME_TIER_2;
