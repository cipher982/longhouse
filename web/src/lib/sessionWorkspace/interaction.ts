import type { AgentSession } from "../../services/api/agents";
import { getLaunchProviderSupport, getProviderLabel } from "../providers";
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
  isViewingHead = true,
  headThreadSession = null,
}: {
  session: AgentSession;
  isViewingHead?: boolean;
  headThreadSession?: Pick<AgentSession, "origin_label" | "environment"> | null;
}): SessionInteractionCapabilities {
  const providerLabel = getProviderLabel(session.provider);
  if (!session.capabilities) {
    throw new Error("Session workspace interactions require session.capabilities");
  }
  const facts = session.session_state;
  const inputAction = facts.mode === "console"
    ? facts.control.actions.start_turn
    : facts.control.actions.send_input;
  const liveControlAvailable = inputAction?.state === "available";
  const hostReattachAvailable = facts.control.actions.reattach.state === "available";
  const canChatFromBrowser = liveControlAvailable;
  const isManagedLocalSession = facts.control.ownership === "owned";
  const isManagedLocalCodex = session.provider === "codex" && isManagedLocalSession;
  const sourceOriginLabel = getSessionOriginLabel(session);
  const headOriginLabel = headThreadSession ? getSessionOriginLabel(headThreadSession) : null;

  // A Console turn can be blocked while the machine channel is still connected
  // — `execution_target_missing` is exactly that — so connection alone does not
  // decide this. Treat any owned-but-blocked Console session as unavailable so
  // it reaches the typed blocker copy instead of the generic read-only text.
  const consoleTurnBlocked = facts.mode === "console" && !liveControlAvailable;
  const mode: SessionInteractionMode =
    liveControlAvailable
      ? "managed_local"
      : isManagedLocalSession && (facts.control.connection !== "connected" || consoleTurnBlocked)
        ? "managed_local_unavailable"
        : "unsupported";
  const isUnsupportedManagedSession = mode === "unsupported" && isManagedLocalSession;

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
  const resumeAction = facts.control.actions.resume;
  const controlUnavailableDescription = (() => {
    // An ended Helm run is not a control fault. Ending the run clears the
    // durable run id, which rejects every run-bound control head by design, so
    // control reads owned/unknown and this fell through to "Longhouse can't
    // confirm the control link" — a lease diagnostic shown as a warning for
    // the ordinary act of exiting a terminal.
    if (runEnded) {
      return resumeAction.state === "available"
        ? `This ${providerLabel} session's run has ended. Resume it to keep going.`
        : `This ${providerLabel} session's run has ended.`;
    }
    if (facts.mode === "console") {
      switch (facts.control.actions.start_turn?.reason) {
        case "machine_offline":
          return `The machine running this ${providerLabel} session is offline. Sending resumes when it reconnects.`;
        case "adapter_unavailable":
          return `This session's machine isn't accepting new ${providerLabel} turns.`;
        case "execution_target_missing":
          return "Longhouse has no machine and folder recorded to run this in.";
        default:
          return `Longhouse cannot start a new ${providerLabel} turn on this session right now.`;
      }
    }
    // Machine reachability before reattach, matching the server and iOS.
    // Reattach eligibility never consults host state, so an offline machine
    // could otherwise be told to reattach to something it cannot reach.
    if (facts.host.state === "offline" || facts.host.state === "stale") {
      return `The machine running this ${providerLabel} session is offline. Sending resumes when it reconnects.`;
    }
    if (hostReattachAvailable) {
      return `Longhouse isn't attached to this ${providerLabel} session. Reattach to steer it from here.`;
    }
    switch (facts.control.connection) {
      case "degraded":
        return `Longhouse's control link to this ${providerLabel} session stopped answering.`;
      case "disconnected":
        return `Longhouse's control path to this ${providerLabel} session is closed.`;
      default:
        return `Longhouse can't confirm the control link to this ${providerLabel} session right now.`;
    }
  })();
  const controlUnavailableTitle = runEnded
    ? "Run ended"
    : facts.presentation.access?.label?.trim() || "Control is offline";

  const managedLaunchSuggestion =
    mode === "unsupported" && !isManagedLocalSession
      ? getManagedLaunchSuggestion(session.provider)
      : null;
  const unsupportedCapabilityDescription = managedLaunchSuggestion
    ? `Longhouse can search this unmanaged ${providerLabel} session here, but it cannot steer it.`
    : isUnsupportedManagedSession
      ? `This managed ${providerLabel} session is read-only because no current control action is available.`
      : `Longhouse can search this unmanaged ${providerLabel} session here, but it cannot steer it.`;
  const unsupportedDescription = managedLaunchSuggestion
    ? `This unmanaged ${providerLabel} session is searchable here, but Longhouse cannot send prompts into it.`
    : isUnsupportedManagedSession
      ? `This managed ${providerLabel} session is read-only because no current control action is available.`
      : `This unmanaged ${providerLabel} session is searchable here, but Longhouse cannot send prompts into it.`;
  const unsupportedManagementDescription = managedLaunchSuggestion
    ? `Longhouse imported this ${providerLabel} session.`
    : `Longhouse imported this ${providerLabel} session.`;

  const managementLabel = isManagedLocalSession ? "Managed" : "Unmanaged";
  const managementDescription = isManagedLocalSession
    ? liveControlAvailable
      ? "Longhouse owns the control path for this session."
      : runEnded
        ? "Longhouse owns this session. Its run has ended."
        : "Longhouse owns this session, but control is currently offline."
    : unsupportedManagementDescription;

  const submitLabel =
    mode === "managed_local"
      ? "Send"
      : "Reply";

  const rawAccessLabel = facts.presentation.access?.label?.trim();
  const capabilityLabel = facts.disposition.state === "closed"
    ? "Closed"
    : runEnded
      ? "Ended"
      : rawAccessLabel || (mode === "managed_local_unavailable" ? "Control unavailable" : "Read only");

  const capabilityVariant =
    mode === "managed_local"
      ? "success"
      // An ended run is an ordinary resting state, not a degraded one. Warning
      // tone here is what made a finished session look broken.
      : mode === "managed_local_unavailable" && !runEnded
        ? "warning"
        : "neutral";

  const capabilityDescription =
    mode === "managed_local"
      ? `Message this live ${providerLabel} session from Longhouse.`
      : mode === "managed_local_unavailable"
        ? controlUnavailableDescription
        : unsupportedCapabilityDescription;

  const title =
    mode === "managed_local"
      ? "Send to session"
      : mode === "managed_local_unavailable"
        ? controlUnavailableTitle
        : "Search and inspect this session";

  const description =
    mode === "managed_local"
      ? `Longhouse can send your next prompt into this live ${providerLabel} session on ${sourceOriginLabel}, and the results sync back into the timeline here.`
      : mode === "managed_local_unavailable"
        ? controlUnavailableDescription
        : unsupportedDescription;

  const serverPlaceholder = session.capabilities.composer_placeholder?.trim();
  const placeholder =
    serverPlaceholder ||
    (mode === "managed_local"
      ? `Send a message to the live ${providerLabel} session...`
      : "Type a message...");

  const keyboardHint = undefined;

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

  const composerDisabledReason =
    mode === "managed_local_unavailable"
      ? notice?.body ?? null
      : mode === "unsupported"
        ? managedLaunchSuggestion
          ? `This unmanaged ${providerLabel} session is read-only in Longhouse.`
          : notice?.body ?? null
        : null;

  return {
    mode,
    providerLabel,
    sourceOriginLabel,
    headOriginLabel,
    isManagedLocalSession,
    isManagedLocalCodex,
    liveControlAvailable,
    hostReattachAvailable,
    canChatFromBrowser,
    managementLabel,
    managementDescription,
    managedLaunchSuggestion,
    capabilityLabel,
    capabilityVariant,
    capabilityDescription,
    composerDisabledReason,
    sendDisabledReason: liveControlAvailable ? null : inputAction?.reason ?? null,
    primaryActionLabel: mode === "managed_local" ? "Open live dock" : "Unavailable",
    submitLabel,
    title,
    description,
    placeholder,
    keyboardHint,
    notice,
  };
}
