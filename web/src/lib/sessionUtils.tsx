/**
 * Pure utility functions for session display and URL state management.
 */

import React from "react";
import { parseUTC } from "./dateUtils";
import { type AgentSession } from "../services/api/agents";
import { resolveSessionRuntimeState } from "./sessionRuntime";

// ---------------------------------------------------------------------------
// Time / date helpers
// ---------------------------------------------------------------------------

export function formatRelativeTime(dateStr: string, nowMs: number = Date.now()): string {
  const date = parseUTC(dateStr);
  const diffMs = nowMs - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) return "Just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 30) return `${diffDays}d ago`;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function getDateKey(dateStr: string, nowMs: number = Date.now()): string {
  const date = parseUTC(dateStr);
  const now = new Date(nowMs);
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);
  const sessionDate = new Date(date.getFullYear(), date.getMonth(), date.getDate());

  if (sessionDate.getTime() === today.getTime()) return "Today";
  if (sessionDate.getTime() === yesterday.getTime()) return "Yesterday";
  return sessionDate.toLocaleDateString(undefined, {
    weekday: "long",
    month: "short",
    day: "numeric",
  });
}

// ---------------------------------------------------------------------------
// Navigation helpers
// ---------------------------------------------------------------------------

export function buildSessionDetailPath(
  session: Pick<AgentSession, "id" | "provider" | "match_event_id">,
  matchEventId?: number | null,
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
    const name = session.git_repo.replace(/\.git$/, "").split("/").pop();
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

function isGeneratedSessionTitle(value: string | null | undefined): value is string {
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
  options: { titleMaxChars?: number; subheadingMaxChars?: number; preferGenerated?: boolean } = {},
): SessionCardText {
  const titleMaxChars = options.titleMaxChars ?? 96;
  const subheadingMaxChars = options.subheadingMaxChars ?? 180;
  const preferGenerated = options.preferGenerated ?? true;
  const firstUser = compactText(session.first_user_message);

  if (preferGenerated && isGeneratedSessionTitle(session.summary_title)) {
    return {
      title: truncateText(compactText(session.summary_title), titleMaxChars),
      titleSource: "generated",
      subheading: firstUser ? truncateText(firstUser, subheadingMaxChars) : null,
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
    title: project && project !== session.provider
      ? `New ${provider} session in ${project}`
      : `New ${provider} session`,
    titleSource: "fallback",
    subheading: null,
  };
}

export function getBranchLabel(value: string | null | undefined): string | null {
  if (!isValidTitle(value)) return null;
  const branch = value!.trim();
  if (branch.toUpperCase() === "HEAD") return null;
  return branch;
}

export function getSessionFallbackSummary(session: AgentSession, maxChars = 180): string {
  const firstUser = compactText(session.first_user_message);
  if (firstUser) {
    return truncateText(firstUser, maxChars);
  }

  const project = getProjectLabel(session);
  const provider = formatProviderName(session.provider);
  if (project && project !== session.provider) {
    return `New ${provider} session in ${project}.`;
  }
  return `New ${provider} session.`;
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
  if (value.toLowerCase() === "gemini") return "Gemini";
  return value.charAt(0).toUpperCase() + value.slice(1);
}

// ---------------------------------------------------------------------------
// Search highlighting
// ---------------------------------------------------------------------------

export function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function renderHighlightedText(text: string, query: string) {
  const tokens = query.split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return text;
  const pattern = tokens.map(escapeRegExp).join("|");
  if (!pattern) return text;
  const splitRegex = new RegExp(`(${pattern})`, "gi");
  const matchRegex = new RegExp(`^(${pattern})$`, "i");

  return text.split(splitRegex).map((part, idx) =>
    matchRegex.test(part) ? (
      <mark key={`${idx}-${part}`} className="search-highlight">
        {part}
      </mark>
    ) : (
      part
    )
  );
}

// ---------------------------------------------------------------------------
// Runtime display helpers
// ---------------------------------------------------------------------------

export function getRuntimeMetaLabel(
  runtime: ReturnType<typeof resolveSessionRuntimeState>,
  relativeNowMs?: number,
): string | null {
  if (runtime.truthTier === "managed-local") {
    return "Live on host";
  }
  if (runtime.lastLiveAt) {
    if (runtime.truthTier === "stale" || runtime.confidence === "stale") {
      return `Updated ${formatRelativeTime(runtime.lastLiveAt, relativeNowMs)}`;
    }
  }
  return null;
}

export function toTitleCaseWords(value: string): string {
  return value
    .split(/\s+/)
    .filter(Boolean)
    .map((word) => {
      if (word.length <= 3 && word === word.toUpperCase()) {
        return word;
      }
      return word.charAt(0).toUpperCase() + word.slice(1);
    })
    .join(" ");
}

export function getRuntimeOutcomeLabel(
  runtime: ReturnType<typeof resolveSessionRuntimeState>,
): string {
  return runtime.runtimeDisplay.headline;
}

export interface RuntimeDisplayCopy {
  headline: string;
  detail: string | null;
}

export function getRuntimeDisplayCopy(
  runtime: ReturnType<typeof resolveSessionRuntimeState>,
): RuntimeDisplayCopy {
  return {
    headline: runtime.runtimeDisplay.headline,
    detail: runtime.runtimeDisplay.detail,
  };
}

export function getTurnsColor(turns: number): string | undefined {
  if (turns < 5) return undefined;
  if (turns < 15) return "var(--color-brand-primary)";
  if (turns < 30) return "var(--color-brand-accent)";
  return "var(--color-intent-error)";
}

// ---------------------------------------------------------------------------
// URL state helpers
// ---------------------------------------------------------------------------

const PAGE_SIZE = 50;
const DEFAULT_DAYS_BACK = 14;
const DEFAULT_SORT_ORDER = "relevant" as const;

export type SortOrder = "relevant" | "recent";

export interface SessionsUrlState {
  project: string;
  provider: string;
  deviceId: string;
  hideAutonomous: boolean;
  daysBack: number;
  searchQuery: string;
  aiSearch: boolean;
  sortOrder: SortOrder;
  limit: number;
}

export function parsePositiveIntParam(
  rawValue: string | null,
  fallback: number,
  min: number = 1,
  max: number = Number.POSITIVE_INFINITY,
): number {
  if (rawValue == null || rawValue.trim() === "") return fallback;
  const parsed = Number(rawValue);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, Math.floor(parsed)));
}

export function readSessionsUrlState(searchParams: URLSearchParams): SessionsUrlState {
  const mode = searchParams.get("mode");
  const aiSearch =
    mode === "hybrid" ||
    mode === "semantic" ||
    mode === "smart" ||
    searchParams.get("semantic") === "1";
  const deviceId = searchParams.get("device_id") || searchParams.get("environment") || "";

  return {
    project: searchParams.get("project") || "",
    provider: searchParams.get("provider") || "",
    deviceId,
    hideAutonomous: searchParams.get("hide_autonomous") !== "false",
    daysBack: parsePositiveIntParam(searchParams.get("days_back"), DEFAULT_DAYS_BACK),
    searchQuery: searchParams.get("query") || "",
    aiSearch,
    sortOrder: searchParams.get("sort") === "recent" ? "recent" : DEFAULT_SORT_ORDER,
    limit: parsePositiveIntParam(searchParams.get("limit"), PAGE_SIZE, PAGE_SIZE, 100),
  };
}

export function buildSessionsSearchParams(state: SessionsUrlState): URLSearchParams {
  const params = new URLSearchParams();

  if (state.project) params.set("project", state.project);
  if (state.provider) params.set("provider", state.provider);
  if (state.deviceId) params.set("device_id", state.deviceId);
  if (state.daysBack !== DEFAULT_DAYS_BACK) params.set("days_back", String(state.daysBack));
  if (state.searchQuery) params.set("query", state.searchQuery);
  if (state.aiSearch) params.set("mode", "hybrid");
  if (state.searchQuery && state.sortOrder !== DEFAULT_SORT_ORDER) params.set("sort", state.sortOrder);
  if (!state.hideAutonomous) params.set("hide_autonomous", "false");
  if (state.limit !== PAGE_SIZE) params.set("limit", String(state.limit));

  return params;
}
