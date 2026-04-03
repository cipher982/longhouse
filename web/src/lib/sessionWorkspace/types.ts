import type { AgentEvent } from "../../services/api/agents";

export type EventFilter = "all" | "messages" | "tools";

export type ToolInteraction = {
  key: string;
  toolName: string;
  callEvent: AgentEvent | null;
  resultEvent: AgentEvent | null;
  pairing: "id" | "fifo" | "orphan" | "pending";
  anchorId: number;
  timestamp: string;
};

export type ToolBatch = {
  key: string;
  interactions: ToolInteraction[];
  timestamp: string;
  anchorId: number;
};

export type TimelineSeam = {
  key: string;
  sessionId: string;
  label: string;
  description: string;
  timestamp: string;
};

export type TimelineItem =
  | { kind: "seam"; seam: TimelineSeam }
  | { kind: "message"; event: AgentEvent }
  | { kind: "tool"; interaction: ToolInteraction }
  | { kind: "tool_batch"; batch: ToolBatch };

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
      parentBatchKey: string | null;
    }
  | {
      kind: "tool_batch";
      key: string;
      rowId: string;
      batch: ToolBatch;
    };

export type TimelineModel = {
  events: AgentEvent[];
  items: TimelineItem[];
  toolItems: ToolInteraction[];
  toolBatches: ToolBatch[];
  selectionMap: Map<string, TimelineSelection>;
  eventIdToSelectionKey: Map<number, string>;
  eventIdToRowId: Map<number, string>;
};

export type SessionInteractionMode =
  | "managed_local"
  | "managed_local_unavailable"
  | "unsupported"
  | "head"
  | "promote"
  | "branch";

export type SessionInteractionCapabilities = {
  mode: SessionInteractionMode;
  providerLabel: string;
  sourceOriginLabel: string;
  headOriginLabel: string | null;
  isManagedLocalSession: boolean;
  isManagedLocalCodex: boolean;
  canDriveManagedLocalSession: boolean;
  canContinueInCloud: boolean;
  canChatFromBrowser: boolean;
  capabilityLabel: string;
  capabilityVariant: "neutral" | "success" | "warning";
  capabilitySummary: string;
  composerDisabledReason: string | null;
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
