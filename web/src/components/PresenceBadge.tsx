/**
 * PresenceBadge — real-time agent presence state indicator.
 *
 * Shows a live dot + label for thinking/running/idle states.
 * Injects keyframe animations once via a module-level flag.
 */

export type PresenceState =
  | "thinking"
  | "running"
  | "idle"
  | "needs_user"
  | "blocked"
  | "stalled"
  | "syncing_transcript";
export type PresenceStateInput = PresenceState | (string & {});

export interface PresenceBadgeProps {
  state: PresenceStateInput | null;
  tool?: string | null;
  /** compact=true renders only the dot, no text label */
  compact?: boolean;
  /** animateCompact=true preserves live motion in dense surfaces like the detail header */
  animateCompact?: boolean;
  className?: string;
  /**
   * showUnknown=true — when state is null, show a dim gray dot with label
   * "Unknown" instead of rendering nothing.
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

ensureStyles();

// ---------------------------------------------------------------------------
// Tool label helpers
// ---------------------------------------------------------------------------

function getToolLabel(tool: string): { prefix: string; label: string } {
  const t = tool.toLowerCase();
  if (t === "bash" || t === "shell" || t === "terminal") return { prefix: "$", label: t };
  if (t === "read") return { prefix: "\u2193", label: "reading" };
  if (t === "write" || t === "edit") return { prefix: "\u270e", label: "writing" };
  if (t === "webfetch" || t === "websearch" || t.includes("fetch") || t.includes("search"))
    return { prefix: "\u2315", label: "fetching" };
  if (t === "task") return { prefix: "\u2699", label: "spawning" };
  // default: show raw tool name with bolt prefix
  return { prefix: "\u26a1", label: tool };
}

function isKnownPresenceState(state: PresenceStateInput | null | undefined): state is PresenceState {
  return (
    state === "thinking" ||
    state === "running" ||
    state === "idle" ||
    state === "needs_user" ||
    state === "blocked" ||
    state === "stalled" ||
    state === "syncing_transcript"
  );
}

function normalizePresenceState(state: PresenceStateInput | null | undefined): PresenceState | null {
  return isKnownPresenceState(state) ? state : null;
}

// ---------------------------------------------------------------------------
// Dot sub-component
// ---------------------------------------------------------------------------

interface DotProps {
  state: PresenceState;
  size: number;
  compact?: boolean;
  animateCompact?: boolean;
}

function Dot({ state, size, compact = false, animateCompact = false }: DotProps) {
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
          background: compact ? "#fb923c" : "radial-gradient(circle, #fb923c 30%, #f97316 100%)",
          animation:
            !compact || animateCompact
              ? "presence-pulse 1.4s ease-in-out infinite"
              : undefined,
          opacity: compact ? 0.88 : 1,
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
          background: compact ? "#38bdf8" : "radial-gradient(circle, #38bdf8 30%, #0ea5e9 100%)",
          animation:
            !compact || animateCompact
              ? "presence-run-blink 0.9s ease-in-out infinite"
              : undefined,
          boxShadow: compact ? undefined : "0 0 6px 2px rgba(56, 189, 248, 0.5)",
          opacity: compact ? 0.88 : 1,
        }}
      />
    );
  }

  if (state === "needs_user") {
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

  if (state === "blocked") {
    return (
      <span
        style={{
          ...base,
          background: compact ? "#dc2626" : "radial-gradient(circle, #f87171 30%, #ef4444 100%)",
          animation: compact ? undefined : "presence-pulse 2.5s ease-in-out infinite",
          opacity: compact ? 0.78 : 1,
          ["--presence-glow" as string]: "rgba(248, 113, 113, 0.5)",
        }}
      />
    );
  }

  if (state === "stalled") {
    return (
      <span
        style={{
          ...base,
          background: compact ? "#b45309" : "radial-gradient(circle, #f59e0b 30%, #b45309 100%)",
          opacity: compact ? 0.84 : 1,
          boxShadow: compact ? undefined : "0 0 6px 2px rgba(245, 158, 11, 0.35)",
        }}
      />
    );
  }

  if (state === "syncing_transcript") {
    return (
      <span
        style={{
          ...base,
          background: compact ? "#a78bfa" : "radial-gradient(circle, #a78bfa 30%, #7c3aed 100%)",
          animation:
            !compact || animateCompact
              ? "presence-pulse 1.6s ease-in-out infinite"
              : undefined,
          opacity: compact ? 0.85 : 1,
          ["--presence-glow" as string]: "rgba(167, 139, 250, 0.5)",
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

export function PresenceBadge({
  state,
  tool,
  compact = false,
  animateCompact = false,
  className,
  showUnknown = false,
}: PresenceBadgeProps) {
  const normalizedState = normalizePresenceState(state);
  const hasUnknownState = state != null && normalizedState == null;

  // null/no signal (or unsupported state) — render only an explicit unknown indicator.
  if (normalizedState === null) {
    if (!showUnknown && !hasUnknownState) return null;

    const unknownLabel = hasUnknownState ? `Unknown (${state})` : "Unknown";

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
        <span className={className} title={unknownLabel} style={{ display: "inline-flex", alignItems: "center" }}>
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
        <span style={{ color: "var(--text-tertiary, #6b7280)", fontWeight: 400 }}>{unknownLabel}</span>
      </span>
    );
  }

  const dotSize = compact ? 8 : 10;

  if (compact) {
    const compactTitle =
      normalizedState === "running" && tool
        ? `Running: ${tool}`
        : normalizedState === "blocked" && tool
          ? `Blocked: ${tool}`
          : normalizedState === "needs_user"
            ? "Idle"
            : normalizedState === "stalled"
              ? "Stalled"
              : normalizedState === "syncing_transcript"
                ? "Syncing transcript"
                : normalizedState;
    return (
      <span
        className={className}
        title={compactTitle}
        style={{ display: "inline-flex", alignItems: "center" }}
      >
        <Dot
          state={normalizedState}
          size={dotSize}
          compact
          animateCompact={animateCompact}
        />
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

  if (normalizedState === "thinking") {
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

  if (normalizedState === "running") {
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

  if (normalizedState === "needs_user") {
    return (
      <span className={className} style={containerStyle}>
        <Dot state="needs_user" size={dotSize} />
        <span style={{ color: "#9ca3af", fontWeight: 500, letterSpacing: "0.02em" }}>
          Idle
        </span>
      </span>
    );
  }

  if (normalizedState === "blocked") {
    const toolLabel = tool ? getToolLabel(tool) : null;
    return (
      <span className={className} style={containerStyle}>
        <Dot state="blocked" size={dotSize} />
        <span
          style={{
            color: "#f87171",
            fontWeight: 500,
            letterSpacing: "0.02em",
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
          }}
        >
          {toolLabel ? (
            <>
              <span style={{ opacity: 0.75, fontSize: 11 }}>{toolLabel.prefix}</span>
              <span>blocked ({toolLabel.label})</span>
            </>
          ) : (
            "Needs permission"
          )}
        </span>
      </span>
    );
  }

  if (normalizedState === "stalled") {
    return (
      <span className={className} style={containerStyle}>
        <Dot state="stalled" size={dotSize} />
        <span style={{ color: "#f59e0b", fontWeight: 600, letterSpacing: "0.02em" }}>
          Stalled
        </span>
      </span>
    );
  }

  if (normalizedState === "syncing_transcript") {
    return (
      <span className={className} style={containerStyle}>
        <Dot state="syncing_transcript" size={dotSize} />
        <span style={{ color: "#a78bfa", fontWeight: 500, letterSpacing: "0.02em" }}>
          Syncing transcript
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
  state: PresenceStateInput | null;
  tool?: string | null;
  className?: string;
}

export function PresenceHero({ state, tool, className }: PresenceHeroProps) {
  const normalizedState = normalizePresenceState(state);
  if (normalizedState === null) return null;

  const isThinking = normalizedState === "thinking";
  const isRunning = normalizedState === "running";
  const isBlocked = normalizedState === "blocked";
  const isStalled = normalizedState === "stalled";
  const isSyncing = normalizedState === "syncing_transcript";

  const borderColor = isThinking
    ? "rgba(251, 146, 60, 0.4)"
    : isRunning
      ? "rgba(56, 189, 248, 0.4)"
      : isBlocked
        ? "rgba(248, 113, 113, 0.4)"
        : isStalled
          ? "rgba(245, 158, 11, 0.36)"
          : isSyncing
            ? "rgba(167, 139, 250, 0.4)"
            : "rgba(107, 114, 128, 0.2)";

  const bgColor = isThinking
    ? "rgba(251, 146, 60, 0.08)"
    : isRunning
      ? "rgba(56, 189, 248, 0.08)"
      : isBlocked
        ? "rgba(248, 113, 113, 0.06)"
        : isStalled
          ? "rgba(245, 158, 11, 0.08)"
          : isSyncing
            ? "rgba(167, 139, 250, 0.08)"
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
      <PresenceBadge state={normalizedState} tool={tool} compact={false} />
    </div>
  );
}
