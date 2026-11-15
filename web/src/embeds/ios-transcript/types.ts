// The payload Swift encodes (`WebTranscriptPayload` in
// ios/Sources/LonghouseApp/Session/Transcript). Every field is optional here
// because the renderer treats a missing one exactly like an empty one.

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
}

export interface TranscriptPayload {
  errorMessage?: string | null;
  items?: TranscriptItem[];
}

/** Stage timings `renderTranscript` returns synchronously. */
export interface RenderMetrics {
  decode_ms: number;
  html_ms: number;
  dom_ms: number;
}

/** Timings `waitForTranscriptFrame` resolves once the frame painted. */
export interface FrameMetrics {
  raf_ms: number;
  total_ms: number;
}
