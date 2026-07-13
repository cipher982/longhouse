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
  cardDescription: string;
  statusLabel: string;
  archiveVisibility: "live";
  cloudSessionStart: "live";
  hooksSupport: "live" | "none";
  telemetryQuality: "rich" | "structured" | "basic";
};

const LAUNCH_PROVIDER_SUPPORT: Record<LaunchProviderId, LaunchProviderSupport> = {
  claude: {
    id: "claude",
    marketingName: "Claude Code",
    cardDescription: "Launch, send, steer, interrupt, and resume",
    statusLabel: "Full control",
    archiveVisibility: "live",
    cloudSessionStart: "live",
    hooksSupport: "live",
    telemetryQuality: "rich",
  },
  codex: {
    id: "codex",
    marketingName: "Codex CLI",
    cardDescription: "Launch, send, steer, interrupt, and resume",
    statusLabel: "Full control",
    archiveVisibility: "live",
    cloudSessionStart: "live",
    hooksSupport: "none",
    telemetryQuality: "structured",
  },
  opencode: {
    id: "opencode",
    marketingName: "OpenCode",
    cardDescription: "Launch, send, interrupt, and terminate",
    statusLabel: "No steering or resume",
    archiveVisibility: "live",
    cloudSessionStart: "live",
    hooksSupport: "none",
    telemetryQuality: "structured",
  },
  antigravity: {
    id: "antigravity",
    marketingName: "Antigravity CLI",
    cardDescription: "Local launch and send",
    statusLabel: "Limited control",
    archiveVisibility: "live",
    cloudSessionStart: "live",
    hooksSupport: "live",
    telemetryQuality: "structured",
  },
  cursor: {
    id: "cursor",
    marketingName: "Cursor Agent",
    cardDescription: "Launch, send, interrupt, terminate, and resume",
    statusLabel: "No mid-turn steering",
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

/** Ordered list for landing/docs surfaces that need a consistent capability story. */
export function getLaunchProviderSupportList(): LaunchProviderSupport[] {
  return [
    LAUNCH_PROVIDER_SUPPORT.claude,
    LAUNCH_PROVIDER_SUPPORT.codex,
    LAUNCH_PROVIDER_SUPPORT.antigravity,
    LAUNCH_PROVIDER_SUPPORT.opencode,
    LAUNCH_PROVIDER_SUPPORT.cursor,
  ];
}
