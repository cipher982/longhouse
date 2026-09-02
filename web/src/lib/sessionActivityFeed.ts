import type { SessionWorkspaceStreamChange } from "../services/api/agents";

/**
 * Per-frame activity feed for one session workspace stream.
 *
 * Every `workspace_changed` frame the browser receives is a real event on the
 * user's machine: a tool starting, a tool result landing, a provisional text
 * delta from the Codex bridge, an assistant message, or a runtime-state-only
 * wake. The activity strip draws one bar per frame and lets it drift out of a
 * twelve-second window, so a wedged turn flattens instead of pulsing forever.
 *
 * The feed is ref-backed and notifies subscribers directly. It never goes
 * through React state: a Codex burst can deliver several frames a second and
 * re-rendering the whole workspace per frame is exactly what we avoid.
 */
export type ActivityFrameKind =
  | "tool_start"
  | "tool_result"
  | "text_delta"
  | "message"
  | "state";

export interface ActivityFrame {
  /** Monotonic timestamp (performance.now) so drift math never sees clock jumps. */
  at: number;
  kind: ActivityFrameKind;
}

export const ACTIVITY_STRIP_WINDOW_MS = 12_000;

/** Bar height per frame kind, as a fraction of the strip's drawable height. */
export const ACTIVITY_FRAME_WEIGHT: Record<ActivityFrameKind, number> = {
  tool_start: 1,
  message: 0.9,
  tool_result: 0.66,
  text_delta: 0.34,
  state: 0.18,
};

const MAX_FRAMES = 400;

/**
 * A frame without a preview still says what woke the server. Durable ingest is
 * how a Claude turn shows its tool boundaries (it never streams text), so it
 * draws as a result bar; a bare runtime wake is a state tick; a read or title
 * update is bookkeeping and draws nothing (null).
 */
export function classifyWorkspaceChange(
  change: Pick<SessionWorkspaceStreamChange, "transcript_preview"> & { change_kind?: string | null },
): ActivityFrameKind | null {
  const preview = change.transcript_preview;
  if (!preview) {
    switch (change.change_kind) {
      case "ingest":
        return "tool_result";
      case "transcript_preview":
        return "text_delta";
      case "read_update":
      case "title_update":
        return null;
      default:
        return "state";
    }
  }
  if (preview.tool_name) {
    return preview.tool_call_state === "running" ? "tool_start" : "tool_result";
  }
  return preview.is_provisional ? "text_delta" : "message";
}

type ActivityListener = (frame: ActivityFrame | null) => void;

function monotonicNow(): number {
  return typeof performance !== "undefined" && typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

export class SessionActivityFeed {
  private frames: ActivityFrame[] = [];
  private readonly listeners = new Set<ActivityListener>();
  private readonly now: () => number;

  constructor(now: () => number = monotonicNow) {
    this.now = now;
  }

  push(kind: ActivityFrameKind, at: number = this.now()): ActivityFrame {
    const frame: ActivityFrame = { at, kind };
    this.frames.push(frame);
    if (this.frames.length > MAX_FRAMES) {
      this.frames.splice(0, this.frames.length - MAX_FRAMES);
    }
    for (const listener of this.listeners) {
      listener(frame);
    }
    return frame;
  }

  subscribe(listener: ActivityListener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** Frames in arrival order; the strip walks it backwards until it leaves the window. */
  snapshot(): readonly ActivityFrame[] {
    return this.frames;
  }

  /** Drops every frame and wakes subscribers so a strip repaints empty. */
  reset(): void {
    this.frames = [];
    for (const listener of this.listeners) {
      listener(null);
    }
  }
}
