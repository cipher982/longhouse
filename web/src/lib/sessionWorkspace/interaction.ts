import type { AgentSession } from "../../services/api/agents";
import { getProviderLabel, supportsDirectWebContinuation } from "../providers";
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
  const isManagedLocalSession = session.execution_home === "managed_local";
  const canDriveManagedLocalSession = isManagedLocalSession && session.source_runner_id != null;
  const canContinueInCloud = !isManagedLocalSession && supportsDirectWebContinuation(session.provider);
  const isManagedLocalCodex = session.provider === "codex" && isManagedLocalSession;
  const sourceOriginLabel = getSessionOriginLabel(session);
  const headOriginLabel = headThreadSession ? getSessionOriginLabel(headThreadSession) : null;

  const mode: SessionInteractionMode = canDriveManagedLocalSession
    ? "managed_local"
    : isManagedLocalSession
      ? "managed_local_unavailable"
      : !canContinueInCloud
        ? "unsupported"
        : !isViewingHead
          ? "branch"
          : session.continuation_kind === "cloud"
            ? "head"
            : "promote";

  const submitLabel =
    mode === "managed_local"
      ? "Send"
      : mode === "branch"
        ? "Branch in Cloud"
        : mode === "promote"
          ? "Start in Cloud"
          : "Reply";

  const title =
    mode === "managed_local"
      ? `Drive this live ${providerLabel} session`
      : mode === "head"
        ? "Cloud continuation began here"
        : mode === "promote"
          ? "Cloud continuation starts here"
          : mode === "branch"
            ? "New cloud branch starts here"
            : mode === "managed_local_unavailable" && isManagedLocalCodex
              ? "Drive this session from the host Codex terminal"
              : mode === "managed_local_unavailable"
                ? "Drive this session from the host terminal"
                : `This ${providerLabel} transcript is synced, but not resumable from the web yet`;

  const description =
    mode === "managed_local"
      ? `This session is still running on ${sourceOriginLabel}. Messages below are injected into the live ${providerLabel} session on its host and sync back into the timeline here.`
      : mode === "head"
        ? `Earlier turns were synced from ${sourceOriginLabel}. New messages below keep extending this cloud session.`
        : mode === "promote"
          ? `Earlier turns were synced from ${sourceOriginLabel}. Your first message below starts the cloud continuation.`
          : mode === "branch"
            ? `Earlier turns were synced from ${sourceOriginLabel}. Your first message below starts a new cloud branch from this point${headOriginLabel ? ` and leaves the latest ${headOriginLabel} head untouched` : ""}.`
            : mode === "managed_local_unavailable"
              ? `This live ${providerLabel} session is still visible here, but Longhouse cannot inject prompts because the runner bridge metadata is missing. Reattach on the host machine to continue.`
              : `Direct cloud continuation is currently wired for Claude sessions only. This ${providerLabel} transcript is still searchable and auditable here while we close that provider gap.`;

  const placeholder =
    mode === "managed_local"
      ? `Send a message to the live ${providerLabel} session...`
      : mode === "branch"
        ? "Branch from this point in cloud..."
        : mode === "promote"
          ? "Continue this thread in the cloud..."
          : "Type a message...";

  const keyboardHint =
    mode === "branch"
      ? 'Press the "Branch in Cloud" button to confirm the new branch.'
      : mode === "promote"
        ? 'Press the "Start in Cloud" button to confirm the first cloud message.'
        : undefined;

  const notice =
    mode === "managed_local_unavailable"
      ? {
          title: isManagedLocalCodex
            ? "Codex session needs host attach"
            : "Live session needs host attach",
          body: `This live ${providerLabel} session is still searchable here, but Longhouse cannot inject prompts until the runner bridge is present. Reattach on the host machine to continue.`,
        }
      : mode === "unsupported"
        ? {
            title: `Web continuation unavailable for ${providerLabel}`,
            body: `This ${providerLabel} transcript is still fully searchable here, but direct cloud continuation is currently wired for Claude sessions only.`,
          }
        : null;

  const primaryActionLabel =
    mode === "managed_local"
      ? "Drive live session"
      : mode === "managed_local_unavailable"
        ? "Reattach on host"
        : canContinueInCloud
          ? "Continue in cloud"
          : "Latest context";

  return {
    mode,
    providerLabel,
    sourceOriginLabel,
    headOriginLabel,
    isManagedLocalSession,
    isManagedLocalCodex,
    canDriveManagedLocalSession,
    canContinueInCloud,
    canChatFromBrowser: mode === "managed_local" || canContinueInCloud,
    primaryActionLabel,
    submitLabel,
    title,
    description,
    placeholder,
    keyboardHint,
    notice,
  };
}
