/**
 * Provider display utilities — single source of truth for provider colors,
 * icons, labels, and launch-facing capability claims.
 *
 * Colors reference CSS custom properties from styles/tokens.css.
 * Add new providers here when onboarding them.
 */

export type LaunchProviderId = "claude" | "codex" | "opencode" | "antigravity" | "cursor";

export type LaunchProviderSupport = {
  id: LaunchProviderId;
  marketingName: string;
  /**
   * Landing capability matrix. Mirrors
   * server/zerg/config/managed_provider_contracts.json — launchAndSend folds
   * launch_local + send_input, interrupt folds interrupt + terminate.
   */
  launchAndSend: boolean;
  interrupt: boolean;
  steerMidTurn: boolean;
  resume: boolean;
  archiveVisibility: "live";
  cloudSessionStart: "live";
  hooksSupport: "live" | "none";
  telemetryQuality: "rich" | "structured" | "basic";
};

const LAUNCH_PROVIDER_SUPPORT: Record<LaunchProviderId, LaunchProviderSupport> = {
  claude: {
    id: "claude",
    marketingName: "Claude Code",
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: true,
    resume: true,
    archiveVisibility: "live",
    cloudSessionStart: "live",
    hooksSupport: "live",
    telemetryQuality: "rich",
  },
  codex: {
    id: "codex",
    marketingName: "Codex CLI",
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: true,
    resume: true,
    archiveVisibility: "live",
    cloudSessionStart: "live",
    hooksSupport: "none",
    telemetryQuality: "structured",
  },
  opencode: {
    id: "opencode",
    marketingName: "OpenCode",
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: false,
    resume: false,
    archiveVisibility: "live",
    cloudSessionStart: "live",
    hooksSupport: "none",
    telemetryQuality: "structured",
  },
  antigravity: {
    id: "antigravity",
    marketingName: "Antigravity CLI",
    launchAndSend: true,
    interrupt: false,
    steerMidTurn: false,
    resume: false,
    archiveVisibility: "live",
    cloudSessionStart: "live",
    hooksSupport: "live",
    telemetryQuality: "structured",
  },
  cursor: {
    id: "cursor",
    marketingName: "Cursor Agent",
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: false,
    resume: true,
    archiveVisibility: "live",
    cloudSessionStart: "live",
    hooksSupport: "none",
    telemetryQuality: "structured",
  },
};

/** Map deprecated provider ids to their canonical successor. */
export function canonicalProvider(provider: string): string {
  const key = provider.toLowerCase();
  return key === "gemini" ? "antigravity" : provider;
}

/** CSS variable for a provider's brand color. */
export function getProviderColor(provider: string): string {
  switch (canonicalProvider(provider)) {
    case "claude":
      return "var(--color-provider-claude)";
    case "codex":
      return "var(--color-provider-codex)";
    case "opencode":
      return "var(--color-provider-opencode)";
    case "antigravity":
      return "var(--color-provider-antigravity)";
    case "cursor":
      return "var(--color-provider-cursor)";
    case "zai":
      return "var(--color-provider-zai)";
    default:
      return "var(--color-provider-default)";
  }
}

/** Proper-cased display names for known providers. */
const PROVIDER_DISPLAY_NAMES: Record<string, string> = {
  claude: "Claude",
  codex: "Codex",
  openai: "OpenAI",
  opencode: "OpenCode",
  antigravity: "Antigravity",
  cursor: "Cursor",
  zai: "Z.ai",
};

/** Human-readable label for a provider. */
export function getProviderLabel(provider: string): string {
  if (!provider) return "Unknown";
  const key = canonicalProvider(provider).toLowerCase();
  return PROVIDER_DISPLAY_NAMES[key] ?? provider.charAt(0).toUpperCase() + provider.slice(1);
}

/** Launch-facing provider capability contract for the currently supported CLIs. */
export function getLaunchProviderSupport(provider: string): LaunchProviderSupport | null {
  const key = canonicalProvider(provider).toLowerCase();
  return (LAUNCH_PROVIDER_SUPPORT as Record<string, LaunchProviderSupport | undefined>)[key] ?? null;
}

/** Ordered list for landing/docs surfaces, sorted by capability depth. */
export function getLaunchProviderSupportList(): LaunchProviderSupport[] {
  return [
    LAUNCH_PROVIDER_SUPPORT.claude,
    LAUNCH_PROVIDER_SUPPORT.codex,
    LAUNCH_PROVIDER_SUPPORT.cursor,
    LAUNCH_PROVIDER_SUPPORT.opencode,
    LAUNCH_PROVIDER_SUPPORT.antigravity,
  ];
}
