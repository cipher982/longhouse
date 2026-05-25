import type { AgentSession } from "../../services/api/agents";
import type { SessionInteractionCapabilities } from "../../lib/sessionWorkspace";
import { PresenceBadge } from "../PresenceBadge";
import {
  getRuntimeDisplayCopy,
  getRuntimeMetaLabel,
  getRuntimeOutcomeLabel,
} from "../../lib/sessionUtils";
import {
  resolveSessionRuntimeState,
  type KnownPresenceState,
  type SessionRuntimeState,
} from "../../lib/sessionRuntime";

interface SessionRuntimeStripProps {
  session: AgentSession;
  interaction: Pick<
    SessionInteractionCapabilities,
    | "isManagedLocalSession"
    | "liveControlAvailable"
    | "hostReattachAvailable"
    | "sourceOriginLabel"
  >;
  hostLabel?: string | null;
  elapsedLabel?: string | null;
  detailFallback?: string | null;
  variant?: "inline" | "block" | "dock" | "bar";
  testId?: string;
}

function getFallbackCapabilityLabel({
  liveControlAvailable,
  hostReattachAvailable,
  hostLabel,
}: {
  liveControlAvailable: boolean;
  hostReattachAvailable: boolean;
  hostLabel: string;
}): string {
  if (liveControlAvailable) {
    return `Live on ${hostLabel}`;
  }
  if (hostReattachAvailable) {
    return "Control offline";
  }
  return "Read only";
}

function getRuntimeBadgeState(runtime: SessionRuntimeState): KnownPresenceState | null {
  return runtime.presenceState;
}

function shouldAnimateRuntimeBadge(state: KnownPresenceState | null): boolean {
  return (
    state === "thinking" ||
    state === "running" ||
    state === "blocked" ||
    state === "stalled"
  );
}

export function SessionRuntimeStrip({
  session,
  interaction,
  hostLabel,
  elapsedLabel,
  detailFallback,
  variant = "inline",
  testId,
}: SessionRuntimeStripProps) {
  const runtime = resolveSessionRuntimeState(session);
  const runtimeBadgeState = getRuntimeBadgeState(runtime);
  const runtimeBadgeTool = runtime.presenceTool;
  const runtimeDisplay = getRuntimeDisplayCopy(runtime, {
    managedLocal: interaction.isManagedLocalSession,
  });
  const runtimePhase = interaction.isManagedLocalSession
    ? runtimeDisplay.headline
    : getRuntimeOutcomeLabel(runtime);
  const runtimeDetail = interaction.isManagedLocalSession
    ? (runtimeDisplay.detail ?? (runtime.runtimeDisplay ? null : detailFallback))
    : null;
  const runtimeMeta = getRuntimeMetaLabel(runtime);
  const resolvedHostLabel =
    hostLabel?.trim() ||
    session.control?.source_runner_name?.trim() ||
    interaction.sourceOriginLabel ||
    "host";
  const capabilityLabel =
    session.capabilities?.display_label?.trim() ||
    getFallbackCapabilityLabel({
      liveControlAvailable: interaction.liveControlAvailable,
      hostReattachAvailable: interaction.hostReattachAvailable,
      hostLabel: resolvedHostLabel,
    });
  const metaParts = [
    {
      key: "capability",
      label: capabilityLabel,
      className: null,
    },
    runtimeMeta && runtimeMeta !== "Live on host"
      ? {
          key: "runtime",
          label: runtimeMeta,
          className: null,
        }
      : null,
    elapsedLabel
      ? {
          key: "elapsed",
          label: elapsedLabel,
          className: "session-runtime-strip__elapsed",
        }
      : null,
  ].filter((part): part is { key: string; label: string; className: string | null } => part != null);

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
    >
      <div className="session-runtime-strip__presence">
        <PresenceBadge
          state={runtimeBadgeState}
          tool={runtimeBadgeTool}
          compact
          animateCompact={shouldAnimateRuntimeBadge(runtimeBadgeState)}
          showUnknown={
            runtimeBadgeState == null &&
            (runtime.hasSignal || interaction.isManagedLocalSession)
          }
        />
        <div className="session-runtime-strip__copy">
          <span className="session-runtime-strip__headline">{runtimePhase}</span>
          {runtimeDetail ? (
            <span className="session-runtime-strip__detail">{runtimeDetail}</span>
          ) : null}
        </div>
      </div>
      {metaParts.length > 0 ? (
        <div className="session-runtime-strip__meta">
          {metaParts.map((part, index) => (
            <span key={part.key} className="session-runtime-strip__meta-item">
              {index > 0 ? (
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
