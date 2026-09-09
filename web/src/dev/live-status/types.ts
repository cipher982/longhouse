import type {
  AgentSessionProjectionItem,
  AgentSessionWorkspaceResponse,
} from "../../services/api/agents";
import type {
  LedgerConnection,
  LedgerReceiptMark,
  LedgerTone,
  SessionLedgerState,
} from "../../components/session-workspace/SessionLedger";

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
export type ReceiptMark = LedgerReceiptMark;
export type RibbonState = SessionLedgerState;
export type RibbonTone = LedgerTone;
export type RibbonConnection = LedgerConnection;

export interface ReplayFrame {
  ribbon: RibbonState;
  items: AgentSessionProjectionItem[];
  explanation: string;
  sourceLabel: string;
  notice: string | null;
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
