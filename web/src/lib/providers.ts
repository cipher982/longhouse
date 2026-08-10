/**
 * Provider display utilities — single source of truth for provider colors,
 * icons, labels, and launch-facing capability claims.
 *
 * Colors reference CSS custom properties from styles/tokens.css.
 * Add new providers here when onboarding them.
 */

import { GENERATED_PROVIDER_CAPABILITIES } from "../generated/provider-capabilities";
import { lookupProviderBrand, providerDisplayName } from "../generated/provider-brands";

export type LaunchProviderId = "claude" | "codex" | "opencode" | "antigravity" | "cursor" | "pi";

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
  /**
   * The `longhouse <id>` command, when the native device facade actually offers
   * one. Sourced from config/native_device_entrypoints.json, NOT from the
   * contract capability flags: a provider can support launch_local while its
   * device entrypoint stays excluded, and telling a user to run a command that
   * does not exist is worse than saying nothing.
   */
  nativeLaunchCommand: string | null;
  archiveVisibility: "live";
  cloudSessionStart: "live" | "none";
  hooksSupport: "live" | "none";
  telemetryQuality: "rich" | "structured" | "basic";
};

// Presentation and provenance facts the provider contract cannot answer. The
// capability claims that it CAN answer -- launchAndSend, interrupt,
// steerMidTurn, resume, cloudSessionStart, nativeLaunchCommand -- are generated
// into ../generated/provider-capabilities.ts and merged below, because this
// table drifted from the contract twice (4402f99ea, 6432e21fa) while its own
// header claimed to mirror it.
const LAUNCH_PROVIDER_PRESENTATION: Record<LaunchProviderId, Omit<LaunchProviderSupport, "id" | "marketingName" | "launchAndSend" | "interrupt" | "steerMidTurn" | "resume" | "cloudSessionStart" | "nativeLaunchCommand">> = {
  claude: {
    archiveVisibility: "live",
    hooksSupport: "live",
    telemetryQuality: "rich",
  },
  codex: {
    archiveVisibility: "live",
    hooksSupport: "none",
    telemetryQuality: "structured",
  },
  opencode: {
    archiveVisibility: "live",
    hooksSupport: "none",
    telemetryQuality: "structured",
  },
  antigravity: {
    archiveVisibility: "live",
    hooksSupport: "none",
    telemetryQuality: "structured",
  },
  cursor: {
    archiveVisibility: "live",
    hooksSupport: "live",
    telemetryQuality: "structured",
  },
  pi: {
    archiveVisibility: "live",
    hooksSupport: "none",
    telemetryQuality: "structured",
  },
};

const LAUNCH_PROVIDER_SUPPORT: Record<LaunchProviderId, LaunchProviderSupport> = Object.fromEntries(
  (Object.keys(LAUNCH_PROVIDER_PRESENTATION) as LaunchProviderId[]).map((id) => {
    const generated = GENERATED_PROVIDER_CAPABILITIES[id];
    return [
      id,
      {
        id,
        marketingName: lookupProviderBrand(id).marketingName,
        nativeLaunchCommand: generated.nativeLaunchCommand,
        launchAndSend: generated.launchAndSend,
        interrupt: generated.interrupt,
        steerMidTurn: generated.steerMidTurn,
        resume: generated.resume,
        cloudSessionStart: generated.cloudSessionStart,
        ...LAUNCH_PROVIDER_PRESENTATION[id],
      },
    ];
  }),
) as Record<LaunchProviderId, LaunchProviderSupport>;

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

/** Human-readable label for a provider. */
export function getProviderLabel(provider: string): string {
  return providerDisplayName(provider, "Unknown");
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
    LAUNCH_PROVIDER_SUPPORT.pi,
    LAUNCH_PROVIDER_SUPPORT.antigravity,
  ];
}
