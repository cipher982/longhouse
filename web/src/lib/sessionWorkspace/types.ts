import type { AgentEvent, AgentEventId, AgentSessionTranscriptAction } from "../../services/api/agents";

export type EventFilter = "all" | "messages" | "tools";

export type ToolInteraction = {
  key: string;
  toolName: string;
  callEvent: AgentEvent | null;
  resultEvent: AgentEvent | null;
  pairing: "id" | "fifo" | "orphan" | "pending";
  anchorId: AgentEventId;
  timestamp: string;
};

/**
 * Run of 2+ consecutive exploration-eligible tool calls (search/read/list).
 * Collapses into a semantic summary chip; expand reveals each call.
 */
export type NoiseGroup = {
  key: string;
  interactions: ToolInteraction[];
  timestamp: string;
  anchorId: AgentEventId;
};

export type TimelineSeam = {
  key: string;
  sessionId: string;
  label: string;
  description: string;
  timestamp: string;
};

export type TimelineAction = {
  key: string;
  action: AgentSessionTranscriptAction;
  label: string;
  timestamp: string;
};

export type TimelineItem =
  | { kind: "seam"; seam: TimelineSeam }
  | { kind: "action"; action: TimelineAction }
  | { kind: "message"; event: AgentEvent }
  | { kind: "tool"; interaction: ToolInteraction }
  | { kind: "noise_group"; group: NoiseGroup };

export type TimelineSelection =
  | {
      kind: "message";
      key: string;
      rowId: string;
      event: AgentEvent;
    }
  | {
      kind: "tool";
      key: string;
      rowId: string;
      interaction: ToolInteraction;
      parentGroupKey: string | null;
    }
  | {
      kind: "noise_group";
      key: string;
      rowId: string;
      group: NoiseGroup;
    };

export type TimelineModel = {
  events: AgentEvent[];
  items: TimelineItem[];
  toolItems: ToolInteraction[];
  noiseGroups: NoiseGroup[];
  selectionMap: Map<string, TimelineSelection>;
  eventIdToSelectionKey: Map<AgentEventId, string>;
  eventIdToRowId: Map<AgentEventId, string>;
};

export type SessionInteractionMode =
  | "managed_local"
  | "managed_local_unavailable"
  | "unsupported";

export type ManagedLaunchSuggestion = {
  title: string;
  body: string;
  command: string;
};

export type SessionInteractionCapabilities = {
  mode: SessionInteractionMode;
  providerLabel: string;
  sourceOriginLabel: string;
  headOriginLabel: string | null;
  isManagedLocalSession: boolean;
  isManagedLocalCodex: boolean;
  liveControlAvailable: boolean;
  hostReattachAvailable: boolean;
  canChatFromBrowser: boolean;
  managementLabel: string;
  managementDescription: string;
  managedLaunchSuggestion: ManagedLaunchSuggestion | null;
  capabilityLabel: string;
  capabilityVariant: "neutral" | "success" | "warning";
  capabilityDescription: string | null;
  composerDisabledReason: string | null;
  sendDisabledReason: string | null;
  primaryActionLabel: string;
  submitLabel: string;
  title: string;
  description: string;
  placeholder: string;
  keyboardHint?: string;
  notice: {
    title: string;
    body: string;
  } | null;
};
