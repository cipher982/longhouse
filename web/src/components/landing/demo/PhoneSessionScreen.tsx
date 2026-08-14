import { useEffect, useRef } from "react";
import type { TimelineItem, ToolInteraction } from "../../../lib/sessionWorkspace";
import { getInteractionDisplayInfo, getToolSummary } from "../../../lib/sessionWorkspace";
import { toolResultLine } from "./liveProjection";

interface PhoneSessionTranscript {
  assistantLine?: string;
  sentMessage?: string;
  /**
   * Canonical timeline items (messages + tool rows) derived from the shared
   * sessionWorkspace projection. The phone is a compact view of the same
   * model the web/iOS timeline renders.
   */
  items?: TimelineItem[];
}

export type PhoneRuntimeTone = "waiting" | "starting" | "ready" | "working" | "done" | "failed";

export interface PhoneSessionScreenProps {
  title: string;
  transcript: PhoneSessionTranscript;
  composerText: string;
  composerDisabled?: boolean;
  runtimeLabel?: string;
  runtimeDetail?: string;
  runtimeTone?: PhoneRuntimeTone;
  sendEnabled?: boolean;
  sent: boolean;
  working: boolean;
  machineName?: string;
  onComposerChange?: (value: string) => void;
  onSend: () => void;
}

function SignalGlyphs() {
  return (
    <span className="phone-session-system-icons" aria-hidden="true">
      <svg className="phone-session-signal" viewBox="0 0 18 15" fill="currentColor">
        <rect x="0" y="10" width="3" height="5" rx="1" />
        <rect x="5" y="7" width="3" height="8" rx="1" />
        <rect x="10" y="3" width="3" height="12" rx="1" />
        <rect x="15" y="0" width="3" height="15" rx="1" />
      </svg>
      <svg className="phone-session-wifi" viewBox="0 0 20 15" fill="none">
        <path d="M1.5 4.5C6.5 0.5 13.5 0.5 18.5 4.5" />
        <path d="M4.5 8C7.7 5.5 12.3 5.5 15.5 8" />
        <path d="M8 11.5C9.2 10.6 10.8 10.6 12 11.5" />
        <circle cx="10" cy="14" r="1" fill="currentColor" stroke="none" />
      </svg>
      <svg className="phone-session-battery" viewBox="0 0 27 14" fill="none">
        <rect x="0.75" y="0.75" width="23" height="12.5" rx="3.5" />
        <path d="M26 5v4" />
        <rect x="3" y="3" width="18" height="8" rx="2" fill="currentColor" stroke="none" />
      </svg>
    </span>
  );
}

function WandGlyph() {
  return (
    <svg className="phone-session-wand" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path d="m5 15 9.5-9.5" />
      <path d="m4 4 12 12" />
      <path d="M15.5 1.5v3M14 3h3M3 13.5v3M1.5 15h3" />
    </svg>
  );
}

function BellGlyph() {
  return (
    <svg className="phone-session-bell" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M5 17h14l-1.5-2.5V10a5.5 5.5 0 0 0-11 0v4.5L5 17Z" />
      <path d="M9.5 20h5" />
    </svg>
  );
}

function BackGlyph() {
  return (
    <svg className="phone-session-back" viewBox="0 0 14 24" fill="none" aria-hidden="true">
      <path d="m10.5 3-7 9 7 9" />
    </svg>
  );
}

function toolSubtitle(interaction: ToolInteraction): string {
  return (getToolSummary(interaction) || "").split("\n")[0] ?? "";
}

function ToolRow({ interaction }: { interaction: ToolInteraction }) {
  const info = getInteractionDisplayInfo(interaction);
  const result = toolResultLine(interaction);
  return (
    <div className="phone-session-tool" key={interaction.key}>
      <span className="phone-session-tool-title">{info.displayName}</span>
      <span className="phone-session-tool-subtitle">{toolSubtitle(interaction)}</span>
      {result ? <span className="phone-session-tool-meta">{result}</span> : null}
    </div>
  );
}

export function PhoneSessionScreen({
  title,
  transcript,
  composerText,
  composerDisabled = false,
  sent,
  working,
  machineName = "macbook",
  runtimeLabel = working ? "Working" : "Idle",
  runtimeDetail = working ? undefined : "Waiting for input",
  runtimeTone = working ? "working" : "waiting",
  sendEnabled = true,
  onComposerChange,
  onSend,
}: PhoneSessionScreenProps) {
  const canSend = sendEnabled && composerText.trim().length > 0 && !sent;
  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const transcriptKey = `${sent}:${working}:${transcript.items?.map((item) =>
    item.kind === "tool"
      ? `${item.interaction.key}:${toolResultLine(item.interaction) ?? ""}`
      : item.kind === "message"
        ? `${item.event.id}:${item.event.content_text ?? ""}`
        : "",
  ).join("|") ?? ""}`;

  useEffect(() => {
    const node = transcriptRef.current;
    if (!node) return;
    if (typeof node.scrollTo === "function") {
      node.scrollTo({ top: node.scrollHeight, behavior: "smooth" });
    } else {
      node.scrollTop = node.scrollHeight;
    }
  }, [transcriptKey]);

  return (
    <div className="phone-session-screen">
      <div className="phone-session-transcript" aria-live="polite" ref={transcriptRef}>
        {transcript.assistantLine ? (
          <div className="phone-session-message phone-session-message-assistant">
            {transcript.assistantLine}
          </div>
        ) : null}
        {sent && transcript.sentMessage ? (
          <div className={`phone-session-submitted${working ? " working" : ""}`}>
            <div className="phone-session-message phone-session-message-user">
              <span className="phone-session-bubble">{transcript.sentMessage}</span>
            </div>
            <div className="phone-session-origin">Longhouse</div>
            {working ? <div className="submitted-status">Working…</div> : null}
          </div>
        ) : null}
        {transcript.items?.map((item) => {
          if (item.kind === "tool") {
            return <ToolRow key={item.interaction.key} interaction={item.interaction} />;
          }
          // Only assistant prose is shown: the user's own message already
          // renders above as the submitted bubble.
          if (item.kind === "message" && item.event.role !== "user") {
            return (
              <div
                className="phone-session-message phone-session-message-assistant"
                key={item.event.id}
              >
                {item.event.content_text}
              </div>
            );
          }
          return null;
        })}
      </div>

      <div className="phone-session-statusbar">
        <span className="phone-session-time">9:41</span>
        <SignalGlyphs />
      </div>

      <nav className="phone-session-nav" aria-label="Session navigation">
        <button type="button" className="phone-session-back-button" aria-label="Back">
          <BackGlyph />
        </button>
        <span className="phone-session-title">{title}</span>
        <div className="phone-session-nav-trailing">
          <span className="phone-session-assist">
            <WandGlyph />
            <span>Assist</span>
          </span>
          <button type="button" className="phone-session-bell-button" aria-label="Notifications">
            <BellGlyph />
          </button>
        </div>
      </nav>

      <section className="phone-session-bottom-card" aria-label="Session controls">
        <div className="phone-session-runtime">
          <div className={`phone-session-state is-${runtimeTone}`}>
            <span className="phone-session-state-indicator" aria-hidden="true" />
            <strong>{runtimeLabel}</strong>
            {runtimeDetail ? (
              <>
                <span className="phone-session-state-separator">·</span>
                <span className="phone-session-state-detail">{runtimeDetail}</span>
              </>
            ) : null}
          </div>
          <span className="phone-session-capability">
            <span className="phone-session-capability-dot" aria-hidden="true" />
            {machineName}
          </span>
        </div>

        <div className="phone-session-composer">
          <button type="button" className="phone-session-plus" aria-label="Add attachment">
            +
          </button>
          <textarea
            className="phone-session-input"
            value={composerText}
            placeholder="Send a message to the live session…"
            disabled={composerDisabled}
            onChange={(event) => onComposerChange?.(event.target.value)}
            aria-label="Message to live session"
            rows={4}
          />
          <button
            type="button"
            className={`phone-session-send${canSend ? " is-armed" : ""}`}
            disabled={!canSend}
            onClick={onSend}
            aria-label={sent ? "Message sent" : "Send"}
          >
            ↑
          </button>
        </div>
      </section>
    </div>
  );
}
