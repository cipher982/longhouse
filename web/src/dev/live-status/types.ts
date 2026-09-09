import type {
  AgentSessionProjectionItem,
  AgentSessionWorkspaceResponse,
} from "../../services/api/agents";

export interface SessionCapture {
  schema: "longhouse.live-status-capture.v1";
  capturedAt: string;
  source: { kind: string; url: string; sessionId: string };
  workspace: AgentSessionWorkspaceResponse;
  stream?: Array<{ atMs: number; event: string; data: unknown }>;
  streamError?: string | null;
  note?: string;
}

export type Scene =
  | "recorded"
  | "working"
  | "quiet"
  | "expiry"
  | "reconnect"
  | "machine"
  | "return"
  | "attention"
  | "finished";
export interface ReceiptMark {
  id: string;
  ageMs: number;
  sequence: number;
  replay: boolean;
}
export interface RibbonState {
  tone: "working" | "quiet" | "unknown" | "attention";
  headline: string;
  detail: string | null;
  detailKind: "literal" | "explanation";
  observation: string;
  connection: "connected" | "reconnecting" | "checking" | "recorded";
  animateWork: boolean;
  outputAgeSeconds: number | null;
  heartbeatAgeMs: number | null;
  receiptMarks: ReceiptMark[];
  facts: Array<{ label: string; value: string }>;
}
export interface ReplayFrame {
  ribbon: RibbonState;
  items: AgentSessionProjectionItem[];
  explanation: string;
  sourceLabel: string;
}
export type LiveSurface = "dock" | "ledger" | "island";
export interface LiveWorkRibbonProps {
  state: RibbonState;
  motionTimeMs: number;
  reduceMotion: boolean;
  surface: LiveSurface;
  notice: string | null;
  previewDecision: "allow" | "deny" | null;
  onPreviewDecision: (decision: "allow" | "deny") => void;
}
