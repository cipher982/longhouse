// The row contract: what `presentTranscript` (shared/session/model) produces
// and the renderer draws. Every field is optional here because the renderer
// treats a missing one exactly like an empty one. Native sends served events
// plus an overlay (`TranscriptProjection`), not rows.

import type { AgentSessionProjectionItem, SessionEventBody } from "@/shared/api/agents";
import type { SubagentChild } from "@/shared/session/model/types";

export interface MediaRef {
  sha256?: string;
  url?: string | null;
  blobUrl?: string | null;
  mediaState?: string;
  mimeType?: string | null;
  width?: number | null;
  height?: number | null;
}

export interface ToolCall {
  title?: string;
  subtitle?: string;
  status?: string;
  input?: string | null;
  rawInput?: string | null;
  output?: string | null;
  media?: MediaRef[] | null;
  startedAtMs?: number | null;
  toolIntent?: string | null;
}

export interface DiffLine {
  kind: string;
  text?: string;
}

export interface Subagent {
  sessionId: string;
  label: string;
  toolCalls: number;
  agentType?: string | null;
  description?: string | null;
  liveStatus?: string | null;
  isFork?: boolean | null;
  depth?: number | null;
  durationMs?: number | null;
  totalTokens?: number | null;
}

export interface TurnEnd {
  label: string;
  doneAt: string;
}

export interface AttachmentSummary {
  filename?: string | null;
  mimeType?: string | null;
  byteSize?: number | null;
  /** A small data: URL the phone made from the bytes it is sending. */
  thumbDataUrl?: string | null;
}

export interface TranscriptItem {
  id: string;
  kind: string;
  role?: string | null;
  title?: string | null;
  subtitle?: string | null;
  body?: string | null;
  fullBody?: string | null;
  collapsed?: boolean;
  status?: string | null;
  duration?: string | null;
  input?: string | null;
  output?: string | null;
  calls?: ToolCall[];
  origin?: string | null;
  media?: MediaRef[] | null;
  /** Bounded attachment metadata for optimistic submitted rows; bytes stay native. */
  attachments?: AttachmentSummary[] | null;
  attachedImages?: number | null;
  attachmentThumbs?: string[] | null;
  failurePreview?: string | null;
  diff?: DiffLine[] | null;
  subagents?: Subagent[] | null;
  subagentSummary?: string | null;
  turnEnd?: TurnEnd | null;
  /** Marks a background-completion wake notice. */
  wake?: boolean;
  /** Cursors of events a lite page sent as previews; expanding asks native for the full bodies. */
  bodyCursors?: string[] | null;
  /** "preview", "loading", "failed" or "unavailable" while bodyCursors is set. */
  bodyState?: string | null;
  /** Assistant message phase (commentary vs final_answer). */
  phase?: string | null;
  /** Claude encrypted thinking block without plaintext. */
  reasoningWithheld?: boolean | null;
  /** Structured task notification completion payload. */
  taskNotification?: Record<string, unknown> | null;
  /** Count from "Waiting for N background agents to finish". */
  waitingBackgroundAgents?: number | null;
  /** Provider API error / retry banner metadata. */
  apiError?: Record<string, unknown> | null;
  /** Provider-emitted tool intent caption. */
  toolIntent?: string | null;
  /** Epoch milliseconds when a running tool started (for live timers). */
  startedAtMs?: number | null;
  /** A tool or activity row that trails under a commentary or reasoning leader. */
  narrationTrail?: boolean;
}

/** A row native owns (an optimistic submitted input or a Longhouse notice) and where it stands. */
export interface OverlayRow {
  row: TranscriptItem;
  /** True: stands at its send time. False: after the transcript, in send order. */
  placedAtSendTime: boolean;
  createdAtMs: number;
  /** `submitting`, `working`, `sent`, `queued`, `couldNotConfirm`, `failed` or `needsUserDecision`. */
  phase: string;
}

export interface LiteBodiesPayload {
  bodies?: SessionEventBody[];
  loading?: string[];
  failed?: string[];
  unavailable?: string[];
}

/** Served events and native state: the renderer builds the rows. */
export interface TranscriptProjection {
  items: AgentSessionProjectionItem[];
  subagents?: SubagentChild[];
  serverUrl?: string | null;
  liteBodies?: LiteBodiesPayload;
  overlay?: OverlayRow[];
}

export interface TranscriptPayload {
  errorMessage?: string | null;
  /** Ready rows (tests and the stability harness). */
  items?: TranscriptItem[];
  /** What native sends. Wins over `items` when both are present. */
  projection?: TranscriptProjection;
}

/** Stage timings `renderTranscript` returns synchronously. */
export interface RenderMetrics {
  decode_ms: number;
  present_ms: number;
  html_ms: number;
  dom_ms: number;
  row_count: number;
  latest_item_id: string | null;
}

/** Timings `waitForTranscriptFrame` resolves once the frame painted. */
export interface FrameMetrics {
  raf_ms: number;
  total_ms: number;
}
