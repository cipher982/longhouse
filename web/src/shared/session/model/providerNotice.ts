/**
 * A provider notification is a system row the provider itself displays: a
 * background job finishing (OMP/Pi `custom_message`, Claude task notification).
 * Its served text is a header line followed by the job's own output, which
 * can run to 2000 characters. The transcript shows it as one collapsed row,
 * same shape as the Thinking row: the header as the label, the first line of
 * output as the hint, the full text behind a tap.
 *
 * The web timeline and the iOS transcript document both call this, so the two
 * clients cannot disagree about what a notice says.
 */

const TITLE_MAX_CHARS = 120;
const HINT_MAX_CHARS = 90;
/** The server's elision marker between a long output's header and its tail. */
const ELISION_LINE = "…";

export interface ProviderNoticeSummary {
  /** The row label: the header line, without its closing period. */
  title: string;
  /** First line of output plus how many more follow; null for a bare header. */
  hint: string | null;
  /** What expanding reveals, verbatim; null when the title already says it all. */
  body: string | null;
}

function ellipsize(text: string, maxChars: number): string {
  return text.length <= maxChars ? text : `${text.slice(0, maxChars).trimEnd()}…`;
}

function cleanText(text: string): string {
  return text.trim().replace(/\s+/g, " ");
}

export function summarizeProviderNotice(text: string | null | undefined): ProviderNoticeSummary {
  const lines = (text ?? "").replace(/\r\n?/g, "\n").trimEnd().split("\n");
  const headerIndex = lines.findIndex((line) => line.trim().length > 0);
  if (headerIndex === -1) return { title: "Provider update", hint: null, body: null };

  const header = lines[headerIndex].trim().replace(/\.$/, "");
  if (header.length > TITLE_MAX_CHARS) {
    return { title: ellipsize(header, TITLE_MAX_CHARS), hint: null, body: lines.slice(headerIndex).join("\n").trimEnd() };
  }

  const rest = lines.slice(headerIndex + 1);
  const output = rest.filter((line) => line.trim().length > 0);
  if (output.every((line) => line.trim() === ELISION_LINE)) return { title: header, hint: null, body: null };

  // The server slices a long output's tail by characters, so the line right
  // after its elision marker starts mid-line: a fragment, not a preview.
  const elided = output[0].trim() === ELISION_LINE;
  const preview = output.filter((line) => line.trim() !== ELISION_LINE).slice(elided ? 1 : 0);
  const more = preview.length - 1;
  const first = preview.length > 0 ? ellipsize(preview[0].trim(), HINT_MAX_CHARS) : null;
  return {
    title: header,
    hint: first !== null && more > 0 ? `${first} … ${more} more line${more === 1 ? "" : "s"}` : first,
    body: rest.join("\n").replace(/^(?:[ \t]*\n)+/, "").trimEnd(),
  };
}

export interface CompactionNoticeSummary {
  title: string;
  detail: string | null;
}

function formatCompactTokenCount(rawCount: string): string {
  const value = Number(rawCount);
  if (!Number.isFinite(value) || value <= 0) return `${rawCount} tokens`;
  if (value >= 1000) return `${(value / 1000).toFixed(1).replace(/\.0$/, "")}k tokens`;
  return `${Math.round(value)} tokens`;
}

export function summarizeCompactionNotice(text: string | null | undefined): CompactionNoticeSummary {
  const trimmed = (text ?? "").trim();
  if (!trimmed || trimmed === "Conversation compacted") {
    return { title: "Context compacted", detail: null };
  }
  const microMatch = trimmed.match(/^(?:Context microcompacted|Microcompacted(?:\s+[^[]+?)?)\s*(?:\[(.+)\])?$/i);
  if (microMatch) {
    const tokens: string[] = [];
    for (const part of (microMatch[1] ?? "").split(/\s+/)) {
      const [key, rawValue] = part.split("=", 2);
      if (!rawValue) continue;
      if (key === "trigger") {
        tokens.push(rawValue.charAt(0).toUpperCase() + rawValue.slice(1));
      } else if (key === "tokens_saved" || key === "tokensSaved") {
        tokens.push(`${formatCompactTokenCount(rawValue).replace(/\s+tokens$/, "")} saved`);
      } else if (key === "pre_tokens") {
        tokens.push(formatCompactTokenCount(rawValue));
      } else if (key === "duration_ms") {
        tokens.push(`${rawValue}ms`);
      } else {
        tokens.push(`${key}=${rawValue}`);
      }
    }
    return { title: "Context microcompacted", detail: tokens.length > 0 ? tokens.join(" · ") : null };
  }

  const bracketMatch = trimmed.match(/^Conversation compacted\s+\[(.+)\]$/i);
  if (bracketMatch) {
    const tokens: string[] = [];
    for (const part of bracketMatch[1].split(/\s+/)) {
      const [key, rawValue] = part.split("=", 2);
      if (!rawValue) continue;
      if (key === "trigger") {
        tokens.push(rawValue.charAt(0).toUpperCase() + rawValue.slice(1));
      } else if (key === "pre_tokens") {
        tokens.push(formatCompactTokenCount(rawValue));
      } else if (key === "duration_ms") {
        tokens.push(`${rawValue}ms`);
      } else {
        tokens.push(`${key}=${rawValue}`);
      }
    }
    return { title: "Context compacted", detail: tokens.length > 0 ? tokens.join(" · ") : null };
  }

  const firstLine = trimmed
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((line) => line.trim())
    .find((line) => line.length > 0);
  return {
    title: "Context compacted",
    detail: firstLine ? ellipsize(firstLine, TITLE_MAX_CHARS) : null,
  };
}

export type TaskNotificationEntrySummary = {
  taskId: string | null;
  status: string | null;
  summary: string;
  meta: string | null;
  note: string | null;
  result: string | null;
};

export type TaskNotificationSummary = {
  title: string;
  status: string | null;
  meta: string | null;
  note: string | null;
  result: string | null;
  entries: TaskNotificationEntrySummary[];
};

export type ApiErrorSummary = {
  title: string;
  badge: string | null;
  detail: string | null;
};

function formatCompactTokenUnit(tokens: number): string {
  const safe = Math.max(0, Math.round(tokens));
  if (safe >= 1_000_000) {
    const m = safe / 1_000_000;
    return `${m >= 10 ? Math.round(m) : m.toFixed(1)}M`;
  }
  if (safe >= 1_000) {
    const k = safe / 1_000;
    if (k >= 100) return `${Math.round(k)}k`;
    const r = k.toFixed(1);
    return r.endsWith(".0") ? `${r.slice(0, -2)}k` : `${r}k`;
  }
  return String(safe);
}

function formatDurationCompact(durationMs: number): string {
  const total = Math.max(0, Math.floor(durationMs / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
  if (minutes > 0) return seconds > 0 ? `${minutes}m ${seconds}s` : `${minutes}m`;
  return `${seconds}s`;
}

function buildTaskNotificationEntrySummary(
  entry: Record<string, unknown>,
  fallbackText?: string | null,
): TaskNotificationEntrySummary {
  const taskId = typeof entry.task_id === "string" && entry.task_id.trim() ? entry.task_id.trim() : null;
  const status = typeof entry.status === "string" && entry.status.trim() ? entry.status.trim() : null;
  const rawSummary = typeof entry.summary === "string" ? cleanText(entry.summary) : "";
  const rawFallback = typeof fallbackText === "string" ? cleanText(fallbackText).split("\n")[0] ?? "" : "";
  const summary =
    rawSummary ||
    rawFallback ||
    (status ? `Background task ${status}` : "Background task update");
  const note = typeof entry.note === "string" && cleanText(entry.note) ? cleanText(entry.note) : null;
  const result = typeof entry.result === "string" && entry.result.trim() ? entry.result.trim() : null;

  const metaParts: string[] = [];
  if (typeof entry.duration_ms === "number" && Number.isFinite(entry.duration_ms) && entry.duration_ms >= 0) {
    metaParts.push(formatDurationCompact(entry.duration_ms));
  }
  const tokenCount =
    typeof entry.subagent_tokens === "number" && Number.isFinite(entry.subagent_tokens)
      ? entry.subagent_tokens
      : typeof entry.total_tokens === "number" && Number.isFinite(entry.total_tokens)
        ? entry.total_tokens
        : null;
  if (tokenCount != null && tokenCount > 0) {
    metaParts.push(`${formatCompactTokenUnit(tokenCount)} tokens`);
  }
  if (typeof entry.tool_uses === "number" && Number.isFinite(entry.tool_uses) && entry.tool_uses > 0) {
    metaParts.push(`${entry.tool_uses} ${entry.tool_uses === 1 ? "tool use" : "tool uses"}`);
  }

  return {
    taskId,
    status,
    summary,
    meta: metaParts.length > 0 ? metaParts.join(" · ") : null,
    note,
    result,
  };
}

export function summarizeTaskNotification(
  payload: Record<string, unknown>,
  contentText?: string | null,
): TaskNotificationSummary {
  const rawEntries: Record<string, unknown>[] = [payload];
  if (Array.isArray(payload.additional_items)) {
    for (const extra of payload.additional_items) {
      if (extra && typeof extra === "object") {
        rawEntries.push(extra as Record<string, unknown>);
      }
    }
  }
  const entries = rawEntries.map((entry, idx) =>
    buildTaskNotificationEntrySummary(entry, idx === 0 ? contentText : null),
  );
  const first = entries[0];
  if (entries.length === 1) {
    return {
      title: first.summary,
      status: first.status,
      meta: first.meta,
      note: first.note,
      result: first.result,
      entries,
    };
  }
  const results = entries
    .map((entry) => (entry.result ? `${entry.summary}\n${entry.result}` : null))
    .filter((value): value is string => value !== null);
  return {
    title: `${entries.length} background tasks updated`,
    status: first.status,
    meta: entries
      .map((entry) => entry.meta)
      .filter((value): value is string => value !== null)
      .slice(0, 2)
      .join(" | ") || null,
    note: entries.map((entry) => entry.note).find((value): value is string => value !== null) ?? null,
    result: results.length > 0 ? results.join("\n\n---\n\n") : null,
    entries,
  };
}

export function summarizeApiError(
  payload: Record<string, unknown>,
  contentText?: string | null,
): ApiErrorSummary {
  const attempt = typeof payload.retry_attempt === "number" ? payload.retry_attempt : null;
  const maxRetries = typeof payload.max_retries === "number" ? payload.max_retries : null;
  const statusCode = typeof payload.status_code === "number" ? payload.status_code : null;
  const retryInMs = typeof payload.retry_in_ms === "number" ? payload.retry_in_ms : null;

  let badge: string | null = null;
  if (attempt != null && maxRetries != null) {
    badge = `retry ${attempt}/${maxRetries}`;
  } else if (attempt != null) {
    badge = `retry ${attempt}`;
  }
  if (statusCode != null) {
    badge = badge ? `${badge} · HTTP ${statusCode}` : `HTTP ${statusCode}`;
  }
  if (retryInMs != null && retryInMs > 0) {
    badge = badge ? `${badge} · in ${formatDurationCompact(retryInMs)}` : `retry in ${formatDurationCompact(retryInMs)}`;
  }

  const rawError =
    (typeof payload.error === "string" && payload.error.trim()) ||
    (typeof payload.details === "string" && payload.details.trim()) ||
    (typeof contentText === "string" && contentText.trim()) ||
    "Provider API error";

  const detail =
    typeof payload.details === "string" &&
    payload.details.trim() &&
    typeof payload.error === "string" &&
    payload.details.trim() !== payload.error.trim()
      ? payload.details.trim()
      : null;

  return {
    title: ellipsize(cleanText(rawError), TITLE_MAX_CHARS),
    badge,
    detail,
  };
}
