/**
 * Centralized model configuration for Jarvis.
 *
 * Loads from config/models.json - the single source of truth shared with Python.
 * Bundlers (Vite, Bun) inline this JSON at build time, so it works in browser.
 *
 * Environment variable overrides:
 * - JARVIS_REALTIME_MODEL: Override the TIER_1 realtime model
 * - JARVIS_REALTIME_MODEL_MINI: Override the TIER_2 realtime model
 * - JARVIS_USE_MINI_MODEL: Set to "true" to use TIER_2 model
 * - JARVIS_VOICE: Override default voice
 */

// @ts-expect-error - JSON import works with bundlers, TS may complain without resolveJsonModule
// Path: packages/core/src -> ../../.. = /app -> /app/config/models.json (Docker layout)
// NOTE: Local tests use vitest alias to map this path correctly.
// TODO: Replace with @swarm/config import once Docker builds are updated (Phase 2)
import modelsConfig from '../../../config/models.json';

// =============================================================================
// DERIVED FROM config/models.json - Single source of truth
// =============================================================================

const realtime = modelsConfig.realtime;
const text = modelsConfig.text;
const aliases: Record<string, string> = modelsConfig.realtime.aliases ?? {};
const defaultVoice: string = modelsConfig.realtime.defaultVoice ?? 'verse';

// Realtime model tiers (Jarvis voice interface)
export const REALTIME_TIER_1 = realtime.tiers.TIER_1;
export const REALTIME_TIER_2 = realtime.tiers.TIER_2;

// Text model tiers (for reference, primarily used by Zerg Python backend)
export const TEXT_TIER_1 = text.tiers.TIER_1;
export const TEXT_TIER_2 = text.tiers.TIER_2;
export const TEXT_TIER_3 = text.tiers.TIER_3;

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
  return aliases[model] || model;
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
    defaultVoice: getEnv('JARVIS_VOICE') || defaultVoice,
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
