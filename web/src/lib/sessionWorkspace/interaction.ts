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
    cloud_branch_available: cloudBranchAvailable,
    host_reattach_available: hostReattachAvailable,
  } = session.capabilities;
  const isManagedLocalSession = liveControlAvailable || hostReattachAvailable;
  const isManagedLocalCodex = session.provider === "codex" && isManagedLocalSession;
  const sourceOriginLabel = getSessionOriginLabel(session);
  const headOriginLabel = headThreadSession ? getSessionOriginLabel(headThreadSession) : null;

  const mode: SessionInteractionMode = liveControlAvailable
    ? "managed_local"
    : cloudBranchAvailable
      ? !isViewingHead
        ? "branch"
        : session.continuation_kind === "cloud"
          ? "head"
          : "promote"
      : hostReattachAvailable
        ? "managed_local_unavailable"
        : "unsupported";

  const submitLabel =
    mode === "managed_local"
      ? "Send"
      : mode === "branch"
        ? "Branch from Here"
        : mode === "promote"
          ? "Start Cloud Branch"
          : mode === "head"
            ? "Reply in Cloud"
            : "Reply";

  const capabilityLabel =
    mode === "managed_local"
      ? "Live control"
      : mode === "managed_local_unavailable"
        ? "Reattach on host"
        : cloudBranchAvailable
          ? "Cloud branch"
          : "History only";

  const capabilityVariant =
    mode === "managed_local"
      ? "success"
      : mode === "managed_local_unavailable"
        ? "warning"
        : "neutral";

  const capabilitySummary =
    mode === "managed_local"
      ? `Message this live ${providerLabel} session from Longhouse, or reattach on the host machine.`
      : mode === "managed_local_unavailable"
        ? `This live ${providerLabel} session is visible here, but you need the host terminal to keep driving it.`
        : mode === "head"
          ? "Keep working in this cloud branch from the browser."
          : mode === "promote"
            ? "Start a cloud branch from this session."
            : mode === "branch"
              ? "Start a new cloud branch from this point."
              : `Search and inspect this ${providerLabel} session here; cloud branching is not available from this session yet.`;

  const title =
    mode === "managed_local"
      ? "Continue this session"
      : mode === "head"
        ? "Cloud Branch"
        : mode === "promote"
          ? "Start Cloud Branch"
          : mode === "branch"
            ? "Branch from Here"
            : mode === "managed_local_unavailable"
              ? "Continue this session on the host"
              : "Search and inspect this session";

  const description =
    mode === "managed_local"
      ? `Longhouse can send your next prompt into this live ${providerLabel} session on ${sourceOriginLabel}, and the results sync back into the timeline here.`
      : mode === "head"
        ? `Earlier turns were synced from ${sourceOriginLabel}. New messages below keep working in this cloud branch from Longhouse.`
        : mode === "promote"
          ? `Earlier turns were synced from ${sourceOriginLabel}. Your next message below starts a new cloud branch from this session in Longhouse.`
          : mode === "branch"
            ? `Earlier turns were synced from ${sourceOriginLabel}. Your next message starts a new cloud branch from this point${headOriginLabel ? ` and leaves the latest ${headOriginLabel} head untouched` : ""}.`
            : mode === "managed_local_unavailable"
              ? `This live ${providerLabel} session is still visible here, but Longhouse cannot inject prompts right now. Reattach on the host machine to continue.`
              : `This ${providerLabel} session is fully searchable here, but cloud branching is not available from this session yet.`;

  const placeholder =
    mode === "managed_local"
      ? `Send a message to the live ${providerLabel} session...`
      : mode === "branch"
        ? "Start a cloud branch from this point..."
        : mode === "promote"
          ? "Start a cloud branch from this session..."
          : "Type a message...";

  const keyboardHint =
    mode === "branch"
      ? 'Press the "Branch from Here" button to confirm the new cloud branch.'
      : mode === "promote"
        ? 'Press the "Start Cloud Branch" button to confirm the first cloud-branch message.'
        : undefined;

  const notice =
    mode === "managed_local_unavailable"
        ? {
            title: isManagedLocalCodex ? "Codex session needs host attach" : "Live session needs host attach",
            body: `This live ${providerLabel} session is visible here, but Longhouse cannot reach its host control channel right now. Reattach on the host machine to continue.`,
          }
      : mode === "unsupported"
        ? {
            title: `Cloud branching unavailable for ${providerLabel}`,
            body: `This ${providerLabel} session is still fully searchable here, but cloud branching is not available from this session yet.`,
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
    cloudBranchAvailable,
    hostReattachAvailable,
    canChatFromBrowser: liveControlAvailable || cloudBranchAvailable,
    capabilityLabel,
    capabilityVariant,
    capabilitySummary,
    composerDisabledReason,
    primaryActionLabel: liveControlAvailable ? "Open live dock" : cloudBranchAvailable ? "Open branch dock" : "Unavailable",
    submitLabel,
    title,
    description,
    placeholder,
    keyboardHint,
    notice,
  };
}
