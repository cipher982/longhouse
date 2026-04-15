import type { AgentSession } from "../../services/api/agents";
import type { SessionInteractionCapabilities } from "../../lib/sessionWorkspace";
import { PresenceBadge } from "../PresenceBadge";
import {
  getCardRuntimePhaseLabel,
  getRuntimeDisplayCopy,
  getRuntimeMetaLabel,
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
  variant?: "inline" | "block" | "dock";
  testId?: string;
}

function getCapabilityMeta({
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
    return `Continue on ${hostLabel}`;
  }
  return "Search only";
}

export function SessionRuntimeStrip({
  session,
  interaction,
  hostLabel,
  variant = "inline",
  testId,
}: SessionRuntimeStripProps) {
  const runtime = resolveSessionRuntimeState(session);
  const runtimeDisplay = getRuntimeDisplayCopy(runtime, {
    managedLocal: interaction.isManagedLocalSession,
  });
  const runtimePhase = interaction.isManagedLocalSession
    ? runtimeDisplay.headline
    : runtime.presenceState == null &&
        (runtime.truthTier === "stale" || runtime.truthTier === "inferred")
      ? runtime.status === "completed" || session.ended_at
        ? "Completed"
        : "Recent progress"
      : getCardRuntimePhaseLabel(runtime);
  const runtimeDetail = interaction.isManagedLocalSession
    ? runtimeDisplay.detail
    : null;
  const runtimeMeta = getRuntimeMetaLabel(runtime);
  const resolvedHostLabel =
    hostLabel?.trim() ||
    session.control?.source_runner_name?.trim() ||
    interaction.sourceOriginLabel ||
    "host";
  const metaParts = [
    getCapabilityMeta({
      liveControlAvailable: interaction.liveControlAvailable,
      hostReattachAvailable: interaction.hostReattachAvailable,
      hostLabel: resolvedHostLabel,
    }),
    runtimeMeta && runtimeMeta !== "Live on host" ? runtimeMeta : null,
  ].filter(Boolean);

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
          {metaParts.join(" • ")}
        </div>
      ) : null}
    </div>
  );
}
