import type { AgentSession } from "../../services/api/agents";
import type { SessionInteractionCapabilities } from "../../lib/sessionWorkspace";
import { PresenceBadge } from "../PresenceBadge";
import {
  getCardRuntimePhaseLabel,
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
  const runtimePhase =
    runtime.presenceState == null &&
    (runtime.truthTier === "stale" || runtime.truthTier === "inferred")
      ? runtime.status === "completed" || session.ended_at
        ? "Completed"
        : "Recent progress"
      : getCardRuntimePhaseLabel(runtime);
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
      ].join(" ")}
      data-testid={testId}
    >
      <div className="session-runtime-strip__presence">
        <PresenceBadge
          state={runtime.presenceState}
          tool={runtime.presenceTool}
          compact
          heuristicActive={runtime.heuristicActive}
          showUnknown={interaction.isManagedLocalSession}
        />
        <span className="session-runtime-strip__phase">{runtimePhase}</span>
      </div>
      {metaParts.length > 0 ? (
        <div className="session-runtime-strip__meta">
          {metaParts.join(" • ")}
        </div>
      ) : null}
    </div>
  );
}
