import type { AgentSession } from "@/shared/api/agents";
import { getLaunchProviderSupport, getProviderLabel } from "@/shared/lib/providers";
import type { ManagedLaunchSuggestion, SessionInteractionCapabilities, SessionInteractionMode } from "./types";
import { getSessionOriginLabel } from "./formatters";

function getManagedLaunchSuggestion(provider: string): ManagedLaunchSuggestion | null {
  const support = getLaunchProviderSupport(provider);
  // Gate on the device entrypoint as well as capability flags. A provider may
  // support launch_local while its facade command remains excluded.
  if (!support?.launchAndSend || !support.nativeLaunchCommand) return null;
  return {
    title: `Start the next ${support.marketingName} session through Longhouse`,
    body: `This session stays searchable here. Use this command when you want the next ${support.marketingName} session to stay steerable from Longhouse.`,
    command: support.nativeLaunchCommand,
  };
}

export function getSessionInteractionCapabilities({
  session,
}: {
  session: AgentSession;
}): SessionInteractionCapabilities {
  const providerLabel = getProviderLabel(session.provider);
  if (!session.capabilities) {
    throw new Error("Session workspace interactions require session.capabilities");
  }
  const facts = session.session_state;
  // A launch that has not landed yet is the most fundamental fact about a
  // session: nothing has ever attached to it, so there is no control path for
  // anything to be wrong with. Every branch below this reads a control axis
  // that only means something after a launch succeeded, which is why launch
  // outranks all of them. iOS has ranked it this way since Console launch
  // shipped (`SessionDetail.swift`, `controlBlock`); web read a compat alias
  // for its banner and nothing here, so a starting session drew a spinner and
  // a "Longhouse can't confirm the control link" warning at the same time.
  const launchState = facts.launch?.state ?? null;
  const launchInFlight = launchState === "pending" || launchState === "dispatched";
  const launchFailed = launchState === "failed" || launchState === "abandoned";
  const inputAction = facts.mode === "console"
    ? facts.control.actions.start_turn
    : facts.control.actions.send_input;
  const liveControlAvailable = inputAction?.state === "available";
  const hostReattachAvailable = facts.control.actions.reattach.state === "available";
  const isManagedLocalSession = facts.control.ownership === "owned";
  const sourceOriginLabel = getSessionOriginLabel(session);
  const sourceHostLabel =
    session.control?.source_runner_name?.trim() ||
    session.device_id ||
    "the original machine";

  // A Console turn can be blocked while the machine channel is still connected
  // — `execution_target_missing` is exactly that — so connection alone does not
  // decide this. Treat any owned-but-blocked Console session as unavailable so
  // it reaches the typed blocker copy instead of the generic read-only text.
  const consoleTurnBlocked = facts.mode === "console" && !liveControlAvailable;
  // Launch outranks ownership deliberately. A session whose launch is still in
  // flight may not have been claimed by a control path yet, and "unsupported"
  // tells the user Longhouse cannot steer their session at the one moment it
  // is busy starting it.
  const mode: SessionInteractionMode =
    liveControlAvailable
      ? "managed_local"
      : launchInFlight ||
          launchFailed ||
          (isManagedLocalSession && (facts.control.connection !== "connected" || consoleTurnBlocked))
        ? "managed_local_unavailable"
        : "unsupported";

  // Why sending is unavailable, from the blocker the server already typed.
  // "until the engine reconnects" was asserted for every unavailable state,
  // including Console sessions whose machine was connected the whole time and
  // simply advertised no turn adapter.
  // Closed dominates: a closed session's label already says so, and the server
  // drops its access label for the same reason. Without this a closed session
  // whose last run ended showed a "Run ended" notice beside a "Closed" chip.
  // An unreachable machine also outranks it — that is the fact the user acts on.
  const runEnded =
    facts.mode === "helm" &&
    facts.run?.lifecycle === "ended" &&
    facts.disposition.state !== "closed" &&
    facts.host.state !== "offline" &&
    facts.host.state !== "stale";
  // Why sending is unavailable is the server's sentence
  // (`_control_unavailable_sentence`): closed, unreachable machine, ended run,
  // Console blockers, reattach, then the connection state, in that order.
  // Launch copy stays here because the served reason for a launch is only
  // "Setting up X." or "Launch failed.", without the host or the error.
  const servedDisabledReason = session.capabilities.composer_disabled_reason?.trim() || null;
  const controlUnavailableDescription = (() => {
    if (facts.disposition.state !== "closed" && launchInFlight) {
      return `Longhouse is starting this ${providerLabel} session on ${sourceHostLabel}.`;
    }
    if (facts.disposition.state !== "closed" && launchFailed) {
      const detail = facts.launch?.error_message?.trim();
      return detail
        ? `This ${providerLabel} session did not start: ${detail}`
        : `This ${providerLabel} session did not start on ${sourceHostLabel}.`;
    }
    return servedDisabledReason ?? "Longhouse can't confirm the control link to this session right now.";
  })();
  const controlUnavailableTitle = launchInFlight
    ? "Starting"
    : launchFailed
      ? "Launch failed"
      : runEnded
        ? "Run ended"
        : facts.presentation.access?.label?.trim() || "Control is offline";

  const managedLaunchSuggestion =
    mode === "unsupported" && !isManagedLocalSession
      ? getManagedLaunchSuggestion(session.provider)
      : null;
  const unsupportedCapabilityDescription =
    servedDisabledReason ?? "This imported session is searchable, but Longhouse cannot steer it.";
  const submitLabel =
    mode === "managed_local"
      ? "Send"
      : "Reply";

  const rawAccessLabel = facts.presentation.access?.label?.trim();
  const capabilityLabel = facts.disposition.state === "closed"
    ? "Closed"
    : launchInFlight
      ? "Launching"
      : launchFailed
        ? "Launch failed"
        : runEnded
          ? "Ended"
          : rawAccessLabel || (mode === "managed_local_unavailable" ? "Control unavailable" : "Read only");

  const serverPlaceholder = session.capabilities.composer_placeholder?.trim();
  const placeholder = serverPlaceholder || "Message";

  const notice =
    mode === "managed_local_unavailable"
        ? {
            title: controlUnavailableTitle,
            body: controlUnavailableDescription,
          }
      : mode === "unsupported"
        ? {
            title: isManagedLocalSession
              ? `${providerLabel} session — managed`
              : `${providerLabel} session — unmanaged`,
            body: unsupportedCapabilityDescription,
          }
        : null;

  const composerDisabledReason = mode === "managed_local" ? null : notice?.body ?? null;

  return {
    mode,
    providerLabel,
    sourceOriginLabel,
    isManagedLocalSession,
    hostReattachAvailable,
    managedLaunchSuggestion,
    capabilityLabel,
    composerDisabledReason,
    submitLabel,
    placeholder,
    notice,
  };
}
