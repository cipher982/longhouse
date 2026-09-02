import { useRef } from "react";
import type { AgentSession, SessionTranscriptPreview } from "../../services/api/agents";
import type { SessionInteractionCapabilities } from "../../lib/sessionWorkspace";
import type { SessionActivityFeed } from "../../lib/sessionActivityFeed";
import { ActivityStrip, type ActivityStripTone } from "./ActivityStrip";
import { useWallClock } from "../../hooks/useWallClock";
import {
  getRuntimeDisplayCopy,
  getRuntimeMetaLabel,
  getRuntimeOutcomeLabel,
} from "../../lib/sessionUtils";
import { resolveSessionRuntimeState } from "../../lib/sessionRuntime";

interface SessionRuntimeStripProps {
  session: AgentSession;
  interaction: Pick<
    SessionInteractionCapabilities,
    "mode" | "isManagedLocalSession" | "capabilityLabel"
  >;
  startedLabel?: string | null;
  variant?: "inline" | "block" | "dock" | "bar";
  testId?: string;
  /** Per-frame stream feed; absent when the caller has no live stream. */
  activityFeed?: SessionActivityFeed | null;
}

const TAIL_MAX_CHARS = 160;

function parseIsoMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

/**
 * "31s" while a turn is live so the number itself reads as a heartbeat;
 * "4m" / "2h" once the session has gone quiet and seconds stop mattering.
 */
function formatElapsed(ms: number, fine: boolean): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (fine) {
    if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
    if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
    return `${seconds}s`;
  }
  if (hours >= 24) return `${Math.floor(hours / 24)}d`;
  if (hours > 0) return `${hours}h`;
  if (minutes > 0) return `${minutes}m`;
  return "now";
}

function lastLine(text: string | null | undefined): string | null {
  if (!text) return null;
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter(Boolean);
  const line = lines.at(-1);
  if (!line) return null;
  return line.length > TAIL_MAX_CHARS ? line.slice(line.length - TAIL_MAX_CHARS) : line;
}

function extractCommand(input: unknown): string | null {
  let value = input;
  if (typeof value === "string") {
    try {
      value = JSON.parse(value);
    } catch {
      return null;
    }
  }
  if (value && typeof value === "object" && "command" in value) {
    const command = (value as { command?: unknown }).command;
    if (typeof command === "string" && command.trim()) {
      return command.replace(/\s+/g, " ").trim();
    }
  }
  return null;
}

/** The newest thing the agent is doing, as one mono line. */
function tailFromPreview(preview: SessionTranscriptPreview): string | null {
  if (preview.tool_name && preview.tool_call_state === "running") {
    const command = extractCommand(preview.tool_input_json);
    if (command) return `$ ${command}`;
    const output = lastLine(preview.tool_output_text);
    if (output) return output;
  }
  return lastLine(preview.text);
}

export function SessionRuntimeStrip({
  session,
  interaction,
  startedLabel,
  variant = "inline",
  testId,
  activityFeed = null,
}: SessionRuntimeStripProps) {
  const runtime = resolveSessionRuntimeState(session);
  const facts = runtime.stateFacts;
  const runtimeDisplay = getRuntimeDisplayCopy(runtime);
  const headline = interaction.isManagedLocalSession
    ? runtimeDisplay.headline
    : getRuntimeOutcomeLabel(runtime);
  const runtimeDetail = interaction.isManagedLocalSession ? runtimeDisplay.detail : null;
  const runtimeMeta = getRuntimeMetaLabel(runtime);
  const isClosed = facts.disposition.state === "closed";

  const attention = runtime.tone === "blocked" || runtime.tone === "stalled";
  const stripTone: ActivityStripTone =
    runtime.isExecuting || runtime.tone === "active"
      ? "live"
      : attention
        ? "attention"
        : "idle";
  // Seconds only while a turn is live or waiting on the user; a quiet session
  // ticks once a minute so the whole page is not re-rendered for a number.
  const fine = runtime.isExecuting || attention;
  const nowMs = useWallClock(!isClosed, fine ? 1_000 : 60_000);

  // The primary label's observed_at can refresh with every provisional delta
  // on a streaming provider. Anchor the counter on the earliest observation
  // for the current label+tool so it counts up instead of resetting.
  const primary = facts.presentation.primary ?? null;
  const observedAtMs = parseIsoMs(primary?.observed_at ?? facts.activity.observed_at);
  const anchorKey = `${session.id}:${primary?.key ?? ""}:${facts.activity.tool ?? ""}:${facts.activity.state}`;
  const anchorRef = useRef<{ key: string; startMs: number } | null>(null);
  if (observedAtMs !== null) {
    const current = anchorRef.current;
    anchorRef.current =
      current && current.key === anchorKey
        ? { key: anchorKey, startMs: Math.min(current.startMs, observedAtMs) }
        : { key: anchorKey, startMs: observedAtMs };
  } else if (anchorRef.current && anchorRef.current.key !== anchorKey) {
    anchorRef.current = null;
  }
  const validUntilMs = parseIsoMs(facts.activity.valid_until);
  const elapsedEndMs = fine && validUntilMs !== null && nowMs > validUntilMs ? validUntilMs : nowMs;
  const elapsedLabel =
    anchorRef.current && !isClosed
      ? formatElapsed(elapsedEndMs - anchorRef.current.startMs, fine)
      : null;

  const preview = session.transcript_preview ?? null;
  const tail = runtime.isExecuting && preview && !preview.is_stale ? tailFromPreview(preview) : null;

  // An enabled composer already proves control is live. The label earns its
  // pixels only when something is off: offline, reconnecting, observe-only.
  const showCapabilityChip = interaction.mode !== "managed_local";
  const capabilityChipTone =
    interaction.mode === "managed_local_unavailable" ? "warning" : "neutral";

  const stripTitle =
    runtime.presenceState === "running" && runtime.presenceTool
      ? `Running: ${runtime.presenceTool}`
      : runtime.presenceState === "blocked" && runtime.presenceTool
        ? `Blocked: ${runtime.presenceTool}`
        : headline;

  const metaParts = [
    runtimeMeta && runtimeMeta !== "Live on host"
      ? { key: "runtime", label: runtimeMeta, className: null }
      : null,
    startedLabel
      ? { key: "started", label: startedLabel, className: "session-runtime-strip__started" }
      : null,
  ].filter((part): part is { key: string; label: string; className: string | null } => part != null);
  const showMeta = showCapabilityChip || metaParts.length > 0;

  return (
    <div
      className={[
        "session-runtime-strip",
        `session-runtime-strip--${variant}`,
        `session-runtime-strip--tone-${runtime.tone}`,
        interaction.isManagedLocalSession
          ? "session-runtime-strip--managed"
          : "session-runtime-strip--unmanaged",
      ].join(" ")}
      data-testid={testId}
      data-strip-tone={stripTone}
    >
      <div className="session-runtime-strip__presence">
        <ActivityStrip
          feed={activityFeed}
          tone={stripTone}
          label={`Activity: ${headline}`}
          title={stripTitle}
        />
        <div className="session-runtime-strip__copy">
          <span className="session-runtime-strip__headline">{headline}</span>
          {elapsedLabel ? (
            <span
              className="session-runtime-strip__elapsed"
              data-testid="session-runtime-elapsed"
              aria-label={`Elapsed ${elapsedLabel}`}
            >
              {elapsedLabel}
            </span>
          ) : null}
          {runtimeDetail ? (
            <span className="session-runtime-strip__detail">{runtimeDetail}</span>
          ) : null}
        </div>
      </div>
      {tail ? (
        <div className="session-runtime-strip__tail" data-testid="session-runtime-tail" title={tail}>
          <bdi>{tail}</bdi>
        </div>
      ) : null}
      {showMeta ? (
        <div className="session-runtime-strip__meta">
          {showCapabilityChip ? (
            <span
              className={`session-runtime-strip__chip session-runtime-strip__chip--${capabilityChipTone}`}
              data-testid="session-capability-chip"
            >
              {interaction.capabilityLabel}
            </span>
          ) : null}
          {metaParts.map((part, index) => (
            <span key={part.key} className="session-runtime-strip__meta-item">
              {index > 0 || showCapabilityChip ? (
                <span
                  className="session-runtime-strip__meta-separator"
                  aria-hidden="true"
                >
                  {" "}
                  •{" "}
                </span>
              ) : null}
              <span className={part.className ?? undefined}>{part.label}</span>
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
