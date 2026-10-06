/**
 * Display labels and detail-page paths for a session.
 */

import { type AgentSession } from "@/shared/api/agents";
import { cleanPromptPreview } from "./promptPreview";

// ---------------------------------------------------------------------------
// Navigation helpers
// ---------------------------------------------------------------------------

export function buildSessionDetailPath(
  session: Pick<AgentSession, "id" | "provider" | "match_event_id">,
  matchEventId?: AgentSession["match_event_id"],
): string {
  const params = new URLSearchParams();
  if (matchEventId != null) {
    params.set("event_id", String(matchEventId));
  }
  const search = params.toString();
  return `/timeline/${session.id}${search ? `?${search}` : ""}`;
}

// ---------------------------------------------------------------------------
// Label helpers
// ---------------------------------------------------------------------------

function isValidTitle(name: string | null | undefined): name is string {
  if (!name) return false;
  if (name.length < 3) return false;
  if (name.startsWith("tmp")) return false;
  // Skip git hashes and hex IDs (only hex chars 0-9a-f, 8+ chars)
  // Uses [0-9a-f] not [a-z0-9] to avoid suppressing real names like "longhouse"
  if (/^[0-9a-f]{8,}$/i.test(name)) return false;
  return true;
}

/** Primary identifier: what repo/project/directory is this session for? */
export function getProjectLabel(session: AgentSession): string {
  if (isValidTitle(session.project)) return session.project;
  if (session.cwd) {
    const folder = session.cwd.split("/").pop();
    if (folder && folder.length >= 2) return folder;
  }
  if (session.git_repo) {
    const name = session.git_repo
      .replace(/\.git$/, "")
      .split("/")
      .pop();
    if (name) return name;
  }
  return session.provider;
}

export type SessionCardTitleSource = "generated" | "prompt" | "fallback";

export interface SessionCardText {
  title: string;
  titleSource: SessionCardTitleSource;
  subheading: string | null;
}

function isGeneratedSessionTitle(
  value: string | null | undefined,
): value is string {
  const title = compactText(value);
  if (!title) return false;
  const normalized = title.toLowerCase();
  if (normalized === "untitled session") return false;
  if (normalized === "generating summary") return false;
  if (normalized === "generating title") return false;
  return true;
}

export function getSessionCardText(
  session: AgentSession,
  options: {
    titleMaxChars?: number;
    subheadingMaxChars?: number;
    preferGenerated?: boolean;
  } = {},
): SessionCardText {
  const titleMaxChars = options.titleMaxChars ?? 96;
  const subheadingMaxChars = options.subheadingMaxChars ?? 180;
  const preferGenerated = options.preferGenerated ?? true;
  // The prompt as a one-line preview: paste/attachment/image wrappers named,
  // not printed (promptPreview.ts).
  const firstUser = cleanPromptPreview(session.first_user_message);

  // The server resolves a single sanitized, frozen headline (timeline_title) so
  // iOS/web/widget render identical text and the row stays stable as the live
  // summary drifts. Prefer it; the ladder below is only for pre-anchor payloads.
  // A headline cut from the prompt carries the prompt's wrappers too.
  const resolved =
    session.title_source === "prompt"
      ? cleanPromptPreview(session.timeline_title)
      : compactText(session.timeline_title);
  if (preferGenerated && resolved) {
    // With no title model the server's headline is the first prompt cut to a few
    // words (`title_source: "prompt"`), and the subheading is the whole prompt:
    // the same sentence twice on every row. Render the served headline once,
    // verbatim as on iOS, and drop the echo. A generated title keeps the prompt
    // as its subheading even when it happens to open with the same words.
    if (firstUser && session.title_source === "prompt") {
      return {
        title: truncateText(resolved, titleMaxChars),
        titleSource: "prompt",
        subheading: null,
      };
    }
    return {
      title: truncateText(resolved, titleMaxChars),
      titleSource: "generated",
      subheading: firstUser
        ? truncateText(firstUser, subheadingMaxChars)
        : null,
    };
  }

  if (preferGenerated && isGeneratedSessionTitle(session.summary_title)) {
    return {
      title: truncateText(compactText(session.summary_title), titleMaxChars),
      titleSource: "generated",
      subheading: firstUser
        ? truncateText(firstUser, subheadingMaxChars)
        : null,
    };
  }

  if (firstUser) {
    return {
      title: truncateText(firstUser, titleMaxChars),
      titleSource: "prompt",
      subheading: null,
    };
  }

  const project = getProjectLabel(session);
  const provider = formatProviderName(session.provider);
  return {
    title:
      project && project !== session.provider
        ? `New ${provider} session in ${project}`
        : `New ${provider} session`,
    titleSource: "fallback",
    subheading: null,
  };
}

/**
 * The live, drifting summary title for the subordinate "now:" drift line.
 * Returns null when it's empty or would just echo the frozen headline.
 */
export function getDriftTitle(
  session: Pick<AgentSession, "summary_title">,
  headline: string,
): string | null {
  const drift = compactText(session.summary_title);
  if (!drift) return null;
  // The headline may be truncated ("Foo bar…"); treat the drift as an echo when
  // it equals or starts with the headline's text so a long anchor that matches
  // the live title doesn't surface a redundant "now: …" line.
  const head = compactText(headline)
    .replace(/[…]+$/, "")
    .replace(/\.\.\.$/, "")
    .trim();
  if (head && (drift === head || drift.startsWith(head))) return null;
  return drift;
}

export function getBranchLabel(
  value: string | null | undefined,
): string | null {
  if (!isValidTitle(value)) return null;
  const branch = value!.trim();
  if (branch.toUpperCase() === "HEAD") return null;
  return branch;
}

function compactText(value: string | null | undefined): string {
  return (value || "").trim().replace(/\s+/g, " ");
}

function truncateText(value: string, maxChars: number): string {
  if (value.length <= maxChars) return value;
  return `${value.slice(0, Math.max(0, maxChars - 1)).trimEnd()}...`;
}

function formatProviderName(provider: string | null | undefined): string {
  const value = compactText(provider);
  if (!value) return "agent";
  if (value.toLowerCase() === "codex") return "Codex";
  if (value.toLowerCase() === "claude") return "Claude";
  if (value.toLowerCase() === "antigravity") return "Antigravity";

  return value.charAt(0).toUpperCase() + value.slice(1);
}
