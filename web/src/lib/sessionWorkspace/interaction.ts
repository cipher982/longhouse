import type { AgentSession } from "../../services/api/agents";
import { getProviderLabel } from "../providers";
import type { SessionInteractionCapabilities, SessionInteractionMode } from "./types";
import { getSessionOriginLabel } from "./formatters";

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
  } = session.capabilities;
  const isManagedLocalSession = liveControlAvailable || hostReattachAvailable;
  const isManagedLocalCodex = session.provider === "codex" && isManagedLocalSession;
  const sourceOriginLabel = getSessionOriginLabel(session);
  const headOriginLabel = headThreadSession ? getSessionOriginLabel(headThreadSession) : null;

  const mode: SessionInteractionMode = liveControlAvailable
    ? "managed_local"
    : hostReattachAvailable
      ? "managed_local_unavailable"
      : "unsupported";

  const managementLabel = isManagedLocalSession ? "Managed" : "Unmanaged";
  const managementVariant = isManagedLocalSession ? "success" : "neutral";
  const managementDescription = isManagedLocalSession
    ? liveControlAvailable
      ? "Longhouse owns the live control path for this session."
      : "Longhouse owns this session, but live control currently requires reattaching on the host."
    : `Longhouse imported this ${providerLabel} session, but it does not own the live control path. Launch through Longhouse when you need a managed session.`;

  const submitLabel =
    mode === "managed_local"
      ? "Send"
      : "Reply";

  const capabilityLabel =
    mode === "managed_local"
      ? "Live control"
      : mode === "managed_local_unavailable"
        ? "Reattach on host"
        : "Search only";

  const capabilityVariant =
    mode === "managed_local"
      ? "success"
      : mode === "managed_local_unavailable"
        ? "warning"
        : "neutral";

  const capabilityDescription =
    mode === "managed_local"
      ? `Message this live ${providerLabel} session from Longhouse, or reattach on the host machine.`
      : mode === "managed_local_unavailable"
        ? `This live ${providerLabel} session is visible here, but you need the host terminal to keep driving it.`
        : `This unmanaged ${providerLabel} session is searchable here, but Longhouse cannot steer it from the browser.`;

  const title =
    mode === "managed_local"
      ? "Continue this session"
      : mode === "managed_local_unavailable"
        ? "Continue this session on the host"
        : "Search and inspect this session";

  const description =
    mode === "managed_local"
      ? `Longhouse can send your next prompt into this live ${providerLabel} session on ${sourceOriginLabel}, and the results sync back into the timeline here.`
      : mode === "managed_local_unavailable"
        ? `This managed live ${providerLabel} session is still visible here, but Longhouse cannot inject prompts right now. Reattach on the host machine to continue.`
        : `This unmanaged ${providerLabel} session is searchable here, but Longhouse cannot inject prompts into it.`;

  const placeholder =
    mode === "managed_local"
      ? `Send a message to the live ${providerLabel} session...`
      : "Type a message...";

  const keyboardHint = undefined;

  const notice =
    mode === "managed_local_unavailable"
        ? {
            title: isManagedLocalCodex ? "Codex session needs host attach" : "Live session needs host attach",
            body: `This live ${providerLabel} session is visible here, but Longhouse cannot reach its host control channel right now. Reattach on the host machine to continue.`,
          }
      : mode === "unsupported"
        ? {
            title: `${providerLabel} session — unmanaged`,
            body: `This unmanaged ${providerLabel} session is searchable here, but Longhouse cannot steer it from the browser.`,
          }
        : null;

  const composerDisabledReason =
    mode === "managed_local_unavailable" || mode === "unsupported" ? notice?.body ?? null : null;

  return {
    mode,
    providerLabel,
    sourceOriginLabel,
    headOriginLabel,
    isManagedLocalSession,
    isManagedLocalCodex,
    liveControlAvailable,
    hostReattachAvailable,
    canChatFromBrowser: liveControlAvailable,
    managementLabel,
    managementVariant,
    managementDescription,
    capabilityLabel,
    capabilityVariant,
    capabilityDescription,
    composerDisabledReason,
    primaryActionLabel: liveControlAvailable ? "Open live dock" : "Unavailable",
    submitLabel,
    title,
    description,
    placeholder,
    keyboardHint,
    notice,
  };
}
