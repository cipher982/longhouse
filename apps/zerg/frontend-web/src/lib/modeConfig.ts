/**
 * Centralized mode configuration — ONE place for all mode decisions.
 *
 * Instead of 6+ files independently checking boolean flags, every consumer
 * imports `modeConfig` and reads a semantic property.
 */

export type AppMode = 'dev' | 'demo' | 'production';

export interface ModeConfig {
  mode: AppMode;
  /** How the auth provider should behave */
  authBehavior: 'disabled' | 'synthetic' | 'real';
  /** Show the yellow demo banner at top */
  showDemoBanner: boolean;
  /** Restrict nav to demo-only items (Timeline) */
  demoNavOnly: boolean;
  /** Landing page redirects authenticated users to /timeline */
  landingRedirectsWhenAuthed: boolean;
  /** Include auth credentials in WebSocket connections */
  wsIncludeAuth: boolean;
}

// One lookup table. All mode decisions live HERE.
const MODES: Record<AppMode, Omit<ModeConfig, 'mode'>> = {
  dev: {
    authBehavior: 'disabled',
    showDemoBanner: false,
    demoNavOnly: false,
    landingRedirectsWhenAuthed: false,
    wsIncludeAuth: false,
  },
  demo: {
    authBehavior: 'synthetic',
    showDemoBanner: true,
    demoNavOnly: true,
    landingRedirectsWhenAuthed: false,
    wsIncludeAuth: false,
  },
  production: {
    authBehavior: 'real',
    showDemoBanner: false,
    demoNavOnly: false,
    landingRedirectsWhenAuthed: true,
    wsIncludeAuth: true,
  },
};

export function getModeConfig(mode: AppMode): ModeConfig {
  return { mode, ...MODES[mode] };
}

// Extend window for runtime config injected by backend /config.js
declare global {
  interface Window {
    __APP_MODE__?: string;
  }
}

/**
 * Resolve AppMode from runtime config.
 *
 * Priority:
 *   1. window.__APP_MODE__ (set by backend /config.js)
 *   2. Vite env: VITE_AUTH_ENABLED=false → dev
 *   3. Vite dev server → dev
 *   4. Default: production
 */
function resolveAppMode(): AppMode {
  if (typeof window !== 'undefined' && window.__APP_MODE__) {
    const raw = window.__APP_MODE__.toLowerCase();
    if (raw === 'dev' || raw === 'demo' || raw === 'production') {
      return raw;
    }
  }

  // Vite env fallback for dev mode
  if (import.meta.env.VITE_AUTH_ENABLED === 'false') {
    return 'dev';
  }

  // Development server = dev mode
  if (import.meta.env.MODE === 'development') {
    return 'dev';
  }

  return 'production';
}

/** Singleton mode config — import this everywhere. */
export const modeConfig: ModeConfig = getModeConfig(resolveAppMode());
