// GENERATED FILE - DO NOT EDIT.
// Source: server/zerg/config/managed_provider_contracts.json
//         config/native_device_entrypoints.json
// Regenerate: make generate-provider-capabilities
//
// Only fields the provider contract can answer live here. Marketing name,
// archive visibility, hooks support and telemetry quality have no contract
// counterpart and remain hand-maintained in ../shared/lib/providers.ts.

export type GeneratedProviderId = "antigravity" | "claude" | "codex" | "cursor" | "omp" | "opencode" | "pi";

export type GeneratedProviderCapabilities = {
  readonly id: GeneratedProviderId;
  readonly search: boolean;
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
    search: true,
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: true,
    resume: false,
    cloudSessionStart: "live",
    nativeLaunchCommand: "longhouse antigravity",
  },
  claude: {
    id: "claude",
    search: true,
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: true,
    resume: true,
    cloudSessionStart: "live",
    nativeLaunchCommand: "longhouse claude",
  },
  codex: {
    id: "codex",
    search: true,
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: true,
    resume: true,
    cloudSessionStart: "live",
    nativeLaunchCommand: "longhouse codex",
  },
  cursor: {
    id: "cursor",
    search: true,
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: true,
    resume: true,
    cloudSessionStart: "live",
    nativeLaunchCommand: "longhouse cursor",
  },
  omp: {
    id: "omp",
    search: true,
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: true,
    resume: true,
    cloudSessionStart: "live",
    nativeLaunchCommand: "longhouse omp",
  },
  opencode: {
    id: "opencode",
    search: true,
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: true,
    resume: true,
    cloudSessionStart: "live",
    nativeLaunchCommand: "longhouse opencode",
  },
  pi: {
    id: "pi",
    search: true,
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: true,
    resume: true,
    cloudSessionStart: "live",
    nativeLaunchCommand: "longhouse pi",
  },
};
