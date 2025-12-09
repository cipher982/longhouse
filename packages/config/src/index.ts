import modelsConfig from '../models.json'

export type ModelsConfig = typeof modelsConfig

// Canonical raw config
export { modelsConfig }

// Convenient typed slices
export const realtime = modelsConfig.realtime
export const realtimeTiers = modelsConfig.realtime.tiers
export const realtimeAliases = modelsConfig.realtime.aliases ?? {}
export const defaultRealtimeVoice = modelsConfig.realtime.defaultVoice ?? 'verse'

export const text = modelsConfig.text
export const textTiers = modelsConfig.text.tiers
export const textModels = modelsConfig.text.models
export const textDefaults = modelsConfig.defaults?.text ?? {}
export const realtimeDefaults = modelsConfig.defaults?.realtime ?? {}
export const defaults = modelsConfig.defaults ?? {}

// Path inside the installed package (useful for MODELS_CONFIG_PATH overrides)
export const MODELS_CONFIG_PACKAGE_PATH = new URL('../models.json', import.meta.url).pathname
