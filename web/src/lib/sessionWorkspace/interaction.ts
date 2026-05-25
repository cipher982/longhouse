import type { AgentSession } from "../../services/api/agents";
import { getProviderLabel } from "../providers";
import type { ManagedLaunchSuggestion, SessionInteractionCapabilities, SessionInteractionMode } from "./types";
import { getSessionOriginLabel } from "./formatters";

function getManagedLaunchSuggestion(provider: string, providerLabel: string): ManagedLaunchSuggestion | null {
  if (provider === "claude") {
    return {
      title: "Start the next Claude session through Longhouse",
      body: "This session stays searchable here. Use this command when you want the next Claude session to stay steerable from Longhouse.",
      command: "longhouse claude",
    };
  }
  if (provider === "codex") {
    return {
      title: "Start the next Codex session through Longhouse",
      body: "This session stays searchable here. Use this command when you want the next Codex session to stay steerable from Longhouse.",
      command: "longhouse codex",
    };
  }
  if (provider === "antigravity") {
    return {
      title: "Start the next Antigravity session through Longhouse",
      body: "This session stays searchable here. Use this command when you want the next Antigravity session to have Longhouse ownership and phase signals.",
      command: "longhouse antigravity",
    };
  }
  if (provider === "gemini") {
    return {
      title: "Start the next Google CLI session with Antigravity",
      body: "Legacy Gemini sessions stay searchable here. Use Antigravity for new Google CLI sessions so Longhouse can archive them with managed ownership and phase signals.",
      command: "longhouse antigravity",
    };
  }
  return null;
}

function getManagedLaunchHint(provider: string, providerLabel: string): string {
  if (provider === "gemini") {
    return "Use Antigravity for new Google CLI sessions when you want Longhouse ownership and phase signals.";
  }
  if (provider === "antigravity") {
    return "Launch new Antigravity sessions through Longhouse when you want Longhouse ownership and phase signals.";
  }
  return `Launch new ${providerLabel} sessions through Longhouse when you want to steer them from Longhouse.`;
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
  const {
    live_control_available: liveControlAvailable,
    host_reattach_available: hostReattachAvailable,
    reply_to_live_session_available: replyToLiveSessionAvailable,
  } = session.capabilities;
  const canChatFromBrowser = Boolean(replyToLiveSessionAvailable ?? liveControlAvailable);
  const controlPath = session.runtime_facts?.control_path ?? session.runtime_display?.control_path;
  const isManagedLocalSession =
    controlPath === "managed"
      ? true
      : controlPath === "unmanaged"
        ? false
        : liveControlAvailable || hostReattachAvailable;
  const isManagedLocalCodex = session.provider === "codex" && isManagedLocalSession;
  const sourceOriginLabel = getSessionOriginLabel(session);
  const headOriginLabel = headThreadSession ? getSessionOriginLabel(headThreadSession) : null;
  const genericLaunchHint = getManagedLaunchHint(session.provider, providerLabel);

  const serverInputMode = session.capabilities.input_mode;
  const mode: SessionInteractionMode =
    serverInputMode === "live"
      ? "managed_local"
      : serverInputMode === "offline"
        ? "managed_local_unavailable"
        : serverInputMode === "read_only"
          ? "unsupported"
          : liveControlAvailable
            ? "managed_local"
            : hostReattachAvailable
              ? "managed_local_unavailable"
              : "unsupported";
  const isUnsupportedManagedSession = mode === "unsupported" && isManagedLocalSession;

  const managedLaunchSuggestion =
    mode === "unsupported" && !isManagedLocalSession
      ? getManagedLaunchSuggestion(session.provider, providerLabel)
      : null;
  const unsupportedCapabilityDescription = managedLaunchSuggestion
    ? `Longhouse can search this unmanaged ${providerLabel} session here, but it cannot steer it.`
    : isUnsupportedManagedSession
      ? `This managed ${providerLabel} session is read-only because no current control action is available.`
      : `Longhouse can search this unmanaged ${providerLabel} session here, but it cannot steer it. ${genericLaunchHint}`;
  const unsupportedDescription = managedLaunchSuggestion
    ? `This unmanaged ${providerLabel} session is searchable here, but Longhouse cannot send prompts into it.`
    : isUnsupportedManagedSession
      ? `This managed ${providerLabel} session is read-only because no current control action is available.`
      : `This unmanaged ${providerLabel} session is searchable here, but Longhouse cannot send prompts into it. ${genericLaunchHint}`;
  const unsupportedManagementDescription = managedLaunchSuggestion
    ? `Longhouse imported this ${providerLabel} session.`
    : `Longhouse imported this ${providerLabel} session. ${genericLaunchHint}`;

  const managementLabel = isManagedLocalSession ? "Managed" : "Unmanaged";
  const managementDescription = isManagedLocalSession
    ? liveControlAvailable
      ? "Longhouse owns the control path for this session."
      : "Longhouse owns this session, but control is currently offline."
    : unsupportedManagementDescription;

  const submitLabel =
    mode === "managed_local"
      ? "Send"
      : "Reply";

  const capabilityLabel =
    session.capabilities.display_label?.trim() ||
    (mode === "managed_local"
      ? "Send"
      : mode === "managed_local_unavailable"
        ? "Control offline"
        : "Read only");

  const capabilityVariant =
    mode === "managed_local"
      ? "success"
      : mode === "managed_local_unavailable"
        ? "warning"
        : "neutral";

  const capabilityDescription =
    mode === "managed_local"
      ? `Message this live ${providerLabel} session from Longhouse.`
      : mode === "managed_local_unavailable"
        ? `Longhouse can see this ${providerLabel} session, but cannot send prompts until the engine reconnects.`
        : unsupportedCapabilityDescription;

  const title =
    mode === "managed_local"
      ? "Send to session"
      : mode === "managed_local_unavailable"
        ? "Control is offline"
        : "Search and inspect this session";

  const description =
    mode === "managed_local"
      ? `Longhouse can send your next prompt into this live ${providerLabel} session on ${sourceOriginLabel}, and the results sync back into the timeline here.`
      : mode === "managed_local_unavailable"
        ? `Longhouse can see this ${providerLabel} session, but cannot send prompts until the engine reconnects.`
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
            title: "Control is offline",
            body: `Longhouse can see this ${providerLabel} session, but cannot send prompts until the engine reconnects.`,
          }
      : mode === "unsupported"
        ? {
            title: isManagedLocalSession
              ? `${providerLabel} session — managed`
              : `${providerLabel} session — unmanaged`,
            body: unsupportedCapabilityDescription,
          }
        : null;

  const serverComposerDisabledReason = session.capabilities.composer_disabled_reason?.trim();
  const serverSendDisabledReason = session.capabilities.send_disabled_reason?.trim();
  const composerDisabledReason =
    serverComposerDisabledReason ||
    (mode === "managed_local_unavailable"
      ? notice?.body ?? null
      : mode === "unsupported"
        ? managedLaunchSuggestion
          ? `This unmanaged ${providerLabel} session is read-only in Longhouse.`
          : notice?.body ?? null
        : null);

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
    sendDisabledReason: serverSendDisabledReason || null,
    primaryActionLabel: liveControlAvailable ? "Open live dock" : "Unavailable",
    submitLabel,
    title,
    description,
    placeholder,
    keyboardHint,
    notice,
  };
}
