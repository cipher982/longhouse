/**
 * Provider display utilities — single source of truth for provider colors,
 * icons, labels, and launch-facing capability claims.
 *
 * Colors reference CSS custom properties from styles/tokens.css.
 * Add new providers here when onboarding them.
 */

export type LaunchProviderId = "claude" | "codex" | "opencode" | "antigravity";

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
    cardDescription: "Archive, search, and strongest control path",
    statusLabel: "Strongest today",
    archiveVisibility: "live",
    cloudSessionStart: "live",
    hooksSupport: "live",
    telemetryQuality: "rich",
  },
  codex: {
    id: "codex",
    marketingName: "Codex CLI",
    cardDescription: "Archive, search, and Longhouse launch path",
    statusLabel: "Control-ready",
    archiveVisibility: "live",
    cloudSessionStart: "live",
    hooksSupport: "none",
    telemetryQuality: "structured",
  },
  opencode: {
    id: "opencode",
    marketingName: "OpenCode",
    cardDescription: "Archive, launch, and managed observe",
    statusLabel: "Observe-only today",
    archiveVisibility: "live",
    cloudSessionStart: "live",
    hooksSupport: "none",
    telemetryQuality: "structured",
  },
  antigravity: {
    id: "antigravity",
    marketingName: "Antigravity CLI",
    cardDescription: "Archive, launch, and hook-backed phase signals",
    statusLabel: "Observe-only today",
    archiveVisibility: "live",
    cloudSessionStart: "live",
    hooksSupport: "live",
    telemetryQuality: "structured",
  },
};

/** CSS variable for a provider's brand color. */
export function getProviderColor(provider: string): string {
  switch (provider) {
    case "claude":
      return "var(--color-provider-claude)";
    case "codex":
      return "var(--color-provider-codex)";
    case "opencode":
      return "var(--color-provider-opencode)";
    case "gemini":
      return "var(--color-provider-gemini)";
    case "antigravity":
      return "var(--color-provider-antigravity)";
    case "zai":
      return "var(--color-provider-zai)";
    default:
      return "var(--color-provider-default)";
  }
}

/** Single-letter icon for compact provider badges. */
export function getProviderIcon(provider: string): string {
  switch (provider) {
    case "claude":
      return "C";
    case "codex":
      return "X";
    case "opencode":
      return "O";
    case "gemini":
      return "G";
    case "antigravity":
      return "A";
    case "zai":
      return "Z";
    default:
      return "?";
  }
}

/** Human-readable label for a provider. */
export function getProviderLabel(provider: string): string {
  if (!provider) return "Unknown";
  return provider.charAt(0).toUpperCase() + provider.slice(1);
}

/** Launch-facing provider capability contract for the currently supported CLIs. */
export function getLaunchProviderSupport(provider: string): LaunchProviderSupport | null {
  return (LAUNCH_PROVIDER_SUPPORT as Record<string, LaunchProviderSupport | undefined>)[provider] ?? null;
}

/** Ordered list for landing/docs surfaces that need a consistent capability story. */
export function getLaunchProviderSupportList(): LaunchProviderSupport[] {
  return [
    LAUNCH_PROVIDER_SUPPORT.claude,
    LAUNCH_PROVIDER_SUPPORT.codex,
    LAUNCH_PROVIDER_SUPPORT.antigravity,
    LAUNCH_PROVIDER_SUPPORT.opencode,
  ];
}
