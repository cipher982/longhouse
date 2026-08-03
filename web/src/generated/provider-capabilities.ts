// GENERATED FILE - DO NOT EDIT.
// Source: server/zerg/config/managed_provider_contracts.json
//         config/native_device_entrypoints.json
// Regenerate: make generate-provider-capabilities
//
// Only fields the provider contract can answer live here. Marketing name,
// archive visibility, hooks support and telemetry quality have no contract
// counterpart and remain hand-maintained in ../lib/providers.ts.

export type GeneratedProviderId = "antigravity" | "claude" | "codex" | "cursor" | "opencode";

export type GeneratedProviderCapabilities = {
  readonly id: GeneratedProviderId;
  readonly launchAndSend: boolean;
  readonly interrupt: boolean;
  readonly steerMidTurn: boolean;
  readonly resume: boolean;
  readonly cloudSessionStart: "live" | "none";
  readonly nativeLaunchCommand: string | null;
};

export const GENERATED_PROVIDER_CAPABILITIES: Record<GeneratedProviderId, GeneratedProviderCapabilities> = {
  antigravity: {
    id: "antigravity",
    launchAndSend: false,
    interrupt: false,
    steerMidTurn: false,
    resume: false,
    cloudSessionStart: "none",
    nativeLaunchCommand: null,
  },
  claude: {
    id: "claude",
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: true,
    resume: true,
    cloudSessionStart: "live",
    nativeLaunchCommand: "longhouse claude",
  },
  codex: {
    id: "codex",
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: true,
    resume: true,
    cloudSessionStart: "live",
    nativeLaunchCommand: "longhouse codex",
  },
  cursor: {
    id: "cursor",
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: false,
    resume: true,
    cloudSessionStart: "live",
    nativeLaunchCommand: "longhouse cursor",
  },
  opencode: {
    id: "opencode",
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: false,
    resume: true,
    cloudSessionStart: "live",
    nativeLaunchCommand: "longhouse opencode",
  },
};
