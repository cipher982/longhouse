import type { AgentSession } from "../../services/api/agents";
import type { SessionInteractionCapabilities } from "../../lib/sessionWorkspace";
import { PresenceBadge } from "../PresenceBadge";
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
    | "isManagedLocalSession"
    | "liveControlAvailable"
    | "hostReattachAvailable"
    | "sourceOriginLabel"
  >;
  hostLabel?: string | null;
  elapsedLabel?: string | null;
  detailOverride?: string | null;
  variant?: "inline" | "block" | "dock";
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
  return "Search only";
}

export function SessionRuntimeStrip({
  session,
  interaction,
  hostLabel,
  elapsedLabel,
  detailOverride,
  variant = "inline",
  testId,
}: SessionRuntimeStripProps) {
  const runtime = resolveSessionRuntimeState(session);
  const runtimeDisplay = getRuntimeDisplayCopy(runtime, {
    managedLocal: interaction.isManagedLocalSession,
  });
  const runtimePhase = interaction.isManagedLocalSession
    ? runtimeDisplay.headline
    : getRuntimeOutcomeLabel(runtime, { endedAt: session.ended_at });
  const runtimeDetail = interaction.isManagedLocalSession
    ? (detailOverride ?? runtimeDisplay.detail)
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
          state={runtime.presenceState}
          tool={runtime.presenceTool}
          compact
          animateCompact={interaction.isManagedLocalSession}
          heuristicActive={runtime.heuristicActive}
          showUnknown={interaction.isManagedLocalSession}
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
