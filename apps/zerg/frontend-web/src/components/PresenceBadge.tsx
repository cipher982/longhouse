/**
 * PresenceBadge — real-time agent presence state indicator.
 *
 * Shows a live dot + label for thinking/running/idle states.
 * Injects keyframe animations once via a module-level flag.
 */

import { useEffect } from "react";

export type PresenceState = "thinking" | "running" | "idle";

export interface PresenceBadgeProps {
  state: PresenceState | null;
  tool?: string | null;
  /** compact=true renders only the animated dot, no text label */
  compact?: boolean;
  className?: string;
  /**
   * heuristicActive=true — when state is null, show a dim pulsing green dot
   * with label "Active". Weaker signal than a real presence state but still
   * shows that this session is considered working by the status heuristic.
   */
  heuristicActive?: boolean;
  /**
   * showUnknown=true — when state is null and heuristicActive is false,
   * show a dim gray dot with label "Unknown" instead of rendering nothing.
   * Use this for active sessions that have never emitted a presence signal.
   */
  showUnknown?: boolean;
}

// ---------------------------------------------------------------------------
// Keyframe injection — runs once per page load
// ---------------------------------------------------------------------------

const STYLE_ID = "presence-badge-keyframes";

function ensureStyles() {
  if (typeof document === "undefined") return;
  if (document.getElementById(STYLE_ID)) return;

  const el = document.createElement("style");
  el.id = STYLE_ID;
  el.textContent = `
    @keyframes presence-pulse {
      0%, 100% { opacity: 1; transform: scale(1); box-shadow: 0 0 0 0 var(--presence-glow, rgba(251, 146, 60, 0.7)); }
      50% { opacity: 0.7; transform: scale(1.15); box-shadow: 0 0 0 6px var(--presence-glow, rgba(251, 146, 60, 0)); }
    }
    @keyframes presence-spin {
      0% { transform: rotate(0deg); }
      100% { transform: rotate(360deg); }
    }
    @keyframes presence-dots {
      0%, 20% { content: "."; }
      40% { content: ".."; }
      60%, 100% { content: "..."; }
    }
    @keyframes presence-dot-typing {
      0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
      40% { transform: scale(1); opacity: 1; }
    }
    @keyframes presence-run-blink {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.5; }
    }
  `;
  document.head.appendChild(el);
}

// ---------------------------------------------------------------------------
// Tool label helpers
// ---------------------------------------------------------------------------

function getToolLabel(tool: string): { prefix: string; label: string } {
  const t = tool.toLowerCase();
  if (t === "bash") return { prefix: "$", label: "bash" };
  if (t === "read") return { prefix: "\u2193", label: "reading" };
  if (t === "write" || t === "edit") return { prefix: "\u270e", label: "writing" };
  if (t === "webfetch" || t === "websearch" || t.includes("fetch") || t.includes("search"))
    return { prefix: "\u2315", label: "fetching" };
  if (t === "task") return { prefix: "\u2699", label: "spawning" };
  // default: show raw tool name with bolt prefix
  return { prefix: "\u26a1", label: tool };
}

// ---------------------------------------------------------------------------
// Dot sub-component
// ---------------------------------------------------------------------------

interface DotProps {
  state: PresenceState;
  size: number;
}

function Dot({ state, size }: DotProps) {
  const base: React.CSSProperties = {
    display: "inline-block",
    width: size,
    height: size,
    borderRadius: "50%",
    flexShrink: 0,
  };

  if (state === "thinking") {
    return (
      <span
        style={{
          ...base,
          background: "radial-gradient(circle, #fb923c 30%, #f97316 100%)",
          animation: "presence-pulse 1.4s ease-in-out infinite",
          // CSS custom property for glow color used in keyframes
          ["--presence-glow" as string]: "rgba(251, 146, 60, 0.6)",
        }}
      />
    );
  }

  if (state === "running") {
    return (
      <span
        style={{
          ...base,
          background: "radial-gradient(circle, #38bdf8 30%, #0ea5e9 100%)",
          animation: "presence-run-blink 0.9s ease-in-out infinite",
          boxShadow: "0 0 6px 2px rgba(56, 189, 248, 0.5)",
        }}
      />
    );
  }

  // idle
  return (
    <span
      style={{
        ...base,
        background: "#4b5563",
        opacity: 0.5,
      }}
    />
  );
}

// ---------------------------------------------------------------------------
// Typing dots animation (three bouncing dots for "Thinking...")
// ---------------------------------------------------------------------------

function TypingDots() {
  const dotStyle = (delay: string): React.CSSProperties => ({
    display: "inline-block",
    width: 3,
    height: 3,
    borderRadius: "50%",
    background: "#fb923c",
    margin: "0 1px",
    verticalAlign: "middle",
    animation: `presence-dot-typing 1.2s ease-in-out ${delay} infinite`,
  });

  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 1, marginLeft: 2 }}>
      <span style={dotStyle("0s")} />
      <span style={dotStyle("0.2s")} />
      <span style={dotStyle("0.4s")} />
    </span>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function PresenceBadge({ state, tool, compact = false, className, heuristicActive = false, showUnknown = false }: PresenceBadgeProps) {
  useEffect(() => {
    ensureStyles();
  }, []);

  // null/no signal — fall back to heuristic active or unknown indicator
  if (state === null || state === undefined) {
    if (!heuristicActive) {
      if (!showUnknown) return null;

      // Unknown state: dim gray dot — session appears active but has never emitted signals
      const unknownDotStyle: React.CSSProperties = {
        display: "inline-block",
        width: compact ? 8 : 10,
        height: compact ? 8 : 10,
        borderRadius: "50%",
        flexShrink: 0,
        background: "var(--text-tertiary, #6b7280)",
        opacity: 0.5,
      };

      if (compact) {
        return (
          <span className={className} title="Unknown" style={{ display: "inline-flex", alignItems: "center" }}>
            <span style={unknownDotStyle} />
          </span>
        );
      }

      return (
        <span
          className={className}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            fontSize: 12,
            fontFamily: "var(--font-mono, 'JetBrains Mono', 'Fira Code', monospace)",
            userSelect: "none",
          }}
        >
          <span style={unknownDotStyle} />
          <span style={{ color: "var(--text-tertiary, #6b7280)", fontWeight: 400 }}>Unknown</span>
        </span>
      );
    }

    // Dim pulsing green dot — weaker signal than real presence
    const heuristicDotStyle: React.CSSProperties = {
      display: "inline-block",
      width: compact ? 8 : 10,
      height: compact ? 8 : 10,
      borderRadius: "50%",
      flexShrink: 0,
      background: "radial-gradient(circle, #4ade80 30%, #22c55e 100%)",
      opacity: 0.7,
      animation: "presence-pulse 2s ease-in-out infinite",
      ["--presence-glow" as string]: "rgba(74, 222, 128, 0.4)",
    };

    if (compact) {
      return (
        <span
          className={className}
          title="Active"
          style={{ display: "inline-flex", alignItems: "center" }}
        >
          <span style={heuristicDotStyle} />
        </span>
      );
    }

    return (
      <span
        className={className}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          fontSize: 12,
          fontFamily: "var(--font-mono, 'JetBrains Mono', 'Fira Code', monospace)",
          userSelect: "none",
        }}
      >
        <span style={heuristicDotStyle} />
        <span style={{ color: "#4ade80", fontWeight: 400, opacity: 0.8 }}>Active</span>
      </span>
    );
  }

  const dotSize = compact ? 8 : 10;

  if (compact) {
    return (
      <span
        className={className}
        title={state === "running" && tool ? `Running: ${tool}` : state}
        style={{ display: "inline-flex", alignItems: "center" }}
      >
        <Dot state={state} size={dotSize} />
      </span>
    );
  }

  // Full mode — dot + label
  const containerStyle: React.CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    fontSize: 12,
    fontFamily: "var(--font-mono, 'JetBrains Mono', 'Fira Code', monospace)",
    userSelect: "none",
  };

  if (state === "thinking") {
    return (
      <span className={className} style={containerStyle}>
        <Dot state="thinking" size={dotSize} />
        <span style={{ color: "#fb923c", fontWeight: 500, letterSpacing: "0.02em" }}>
          Thinking
        </span>
        <TypingDots />
      </span>
    );
  }

  if (state === "running") {
    const { prefix, label } = tool ? getToolLabel(tool) : { prefix: "\u26a1", label: "running" };
    return (
      <span className={className} style={containerStyle}>
        <Dot state="running" size={dotSize} />
        <span
          style={{
            color: "#38bdf8",
            fontWeight: 600,
            letterSpacing: "0.03em",
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
          }}
        >
          <span style={{ opacity: 0.75, fontSize: 11 }}>{prefix}</span>
          <span>{label}</span>
        </span>
      </span>
    );
  }

  // idle
  return (
    <span className={className} style={containerStyle}>
      <Dot state="idle" size={dotSize} />
      <span style={{ color: "#6b7280", fontWeight: 400 }}>Idle</span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Larger "hero" variant for the detail panel — wraps PresenceBadge with
// a frosted glass pill and larger typography.
// ---------------------------------------------------------------------------

export interface PresenceHeroProps {
  state: PresenceState | null;
  tool?: string | null;
  className?: string;
}

export function PresenceHero({ state, tool, className }: PresenceHeroProps) {
  useEffect(() => {
    ensureStyles();
  }, []);

  if (state === null || state === undefined) return null;

  const isThinking = state === "thinking";
  const isRunning = state === "running";

  const borderColor = isThinking
    ? "rgba(251, 146, 60, 0.4)"
    : isRunning
      ? "rgba(56, 189, 248, 0.4)"
      : "rgba(107, 114, 128, 0.2)";

  const bgColor = isThinking
    ? "rgba(251, 146, 60, 0.08)"
    : isRunning
      ? "rgba(56, 189, 248, 0.08)"
      : "rgba(107, 114, 128, 0.04)";

  return (
    <div
      className={className}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 10,
        padding: "8px 14px",
        borderRadius: 10,
        border: `1px solid ${borderColor}`,
        background: bgColor,
        backdropFilter: "blur(8px)",
        marginBottom: 12,
      }}
    >
      <PresenceBadge state={state} tool={tool} compact={false} />
    </div>
  );
}
