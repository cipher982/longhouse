/**
 * Provider display utilities — single source of truth for provider colors, icons, and labels.
 *
 * Colors reference CSS custom properties from styles/tokens.css.
 * Add new providers here when onboarding them.
 */

/** CSS variable for a provider's brand color. */
export function getProviderColor(provider: string): string {
  switch (provider) {
    case "claude":
      return "var(--color-provider-claude)";
    case "codex":
      return "var(--color-provider-codex)";
    case "gemini":
      return "var(--color-provider-gemini)";
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
    case "gemini":
      return "G";
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

/** Whether a provider supports cloud session continuation. */
export function supportsCloudContinuation(provider: string): boolean {
  return provider === "claude";
}
