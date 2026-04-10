/**
 * Centralized model configuration.
 *
 * NOTE: These values are inlined from config/models.json to avoid workspace
 * dependency issues in Docker builds. If you update models.json, update here too.
 */

// =============================================================================
// INLINED CONFIG VALUES (from config/models.json)
// =============================================================================

const textConfig = {
  tiers: {
    TIER_1: 'gpt-5.2',
    TIER_2: 'gpt-5-mini',
    TIER_3: 'gpt-5-nano',
  },
};

// =============================================================================
// EXPORTS
// =============================================================================

// Text model tiers (for reference, primarily used by Zerg Python backend)
export const TEXT_TIER_1 = textConfig.tiers.TIER_1;
export const TEXT_TIER_2 = textConfig.tiers.TIER_2;
export const TEXT_TIER_3 = textConfig.tiers.TIER_3;

// Default text model for chat — use this instead of hardcoding model IDs
export const DEFAULT_TEXT_MODEL = TEXT_TIER_1;
