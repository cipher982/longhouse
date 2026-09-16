// GENERATED FILE - DO NOT EDIT.
// Source: server/zerg/config/managed_provider_contracts.json
//         config/native_device_entrypoints.json
// Regenerate: make generate-provider-capabilities
//
// Only fields the provider contract can answer live here. Marketing name,
// archive visibility, hooks support and telemetry quality have no contract
// counterpart and remain hand-maintained in ../lib/providers.ts.

export type GeneratedProviderId = "antigravity" | "claude" | "codex" | "cursor" | "omp" | "opencode" | "pi";

// Landing chips: lit only by live-token factory assertions behind every
// backing operation. The runtime capability fields never light a chip.
export type ProvenChips = {
  readonly search: boolean;
  readonly launchAndSend: boolean;
  readonly interrupt: boolean;
  readonly steerMidTurn: boolean;
  readonly resume: boolean;
};

export type GeneratedProviderCapabilities = {
  readonly id: GeneratedProviderId;
  readonly launchAndSend: boolean;
  readonly interrupt: boolean;
  readonly steerMidTurn: boolean;
  readonly resume: boolean;
  readonly proven: ProvenChips;
  readonly cloudSessionStart: "live" | "none";
  readonly nativeLaunchCommand: string | null;
};

export const GENERATED_PROVIDER_CAPABILITIES: Record<GeneratedProviderId, GeneratedProviderCapabilities> = {
  antigravity: {
    id: "antigravity",
    launchAndSend: true,
    interrupt: false,
    steerMidTurn: false,
    resume: false,
    proven: {
      launchAndSend: false,
      interrupt: false,
      steerMidTurn: false,
      resume: false,
      search: false,
    },
    cloudSessionStart: "none",
    nativeLaunchCommand: "longhouse antigravity",
  },
  claude: {
    id: "claude",
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: true,
    resume: true,
    proven: {
      launchAndSend: false,
      interrupt: false,
      steerMidTurn: false,
      resume: true,
      search: false,
    },
    cloudSessionStart: "live",
    nativeLaunchCommand: "longhouse claude",
  },
  codex: {
    id: "codex",
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: true,
    resume: true,
    proven: {
      launchAndSend: false,
      interrupt: false,
      steerMidTurn: false,
      resume: true,
      search: false,
    },
    cloudSessionStart: "live",
    nativeLaunchCommand: "longhouse codex",
  },
  cursor: {
    id: "cursor",
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: true,
    resume: true,
    proven: {
      launchAndSend: false,
      interrupt: false,
      steerMidTurn: false,
      resume: true,
      search: false,
    },
    cloudSessionStart: "live",
    nativeLaunchCommand: "longhouse cursor",
  },
  omp: {
    id: "omp",
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: true,
    resume: true,
    proven: {
      launchAndSend: true,
      interrupt: true,
      steerMidTurn: true,
      resume: true,
      search: false,
    },
    cloudSessionStart: "live",
    nativeLaunchCommand: "longhouse omp",
  },
  opencode: {
    id: "opencode",
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: false,
    resume: true,
    proven: {
      launchAndSend: false,
      interrupt: false,
      steerMidTurn: false,
      resume: true,
      search: false,
    },
    cloudSessionStart: "live",
    nativeLaunchCommand: "longhouse opencode",
  },
  pi: {
    id: "pi",
    launchAndSend: true,
    interrupt: true,
    steerMidTurn: true,
    resume: true,
    proven: {
      launchAndSend: true,
      interrupt: true,
      steerMidTurn: true,
      resume: true,
      search: false,
    },
    cloudSessionStart: "live",
    nativeLaunchCommand: "longhouse pi",
  },
};
