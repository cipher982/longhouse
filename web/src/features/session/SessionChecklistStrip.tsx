import { useEffect, useId, useState } from "react";
import type { SessionChecklist, SessionChecklistItem } from "@/shared/api/agents";

// Remember expand/collapse per session while navigating around the app.
const expandedBySession = new Map<string, boolean>();

function normalizeStatus(raw: string): "completed" | "in_progress" | "pending" | "skipped" | "failed" {
  const s = (raw || "").trim().toLowerCase();
  if (s === "completed" || s === "done" || s === "complete") return "completed";
  if (s === "in_progress" || s === "in-progress" || s === "running" || s === "active") return "in_progress";
  if (s === "skipped" || s === "cancelled" || s === "canceled") return "skipped";
  if (s === "failed" || s === "error") return "failed";
  return "pending";
}

function statusGlyph(status: ReturnType<typeof normalizeStatus>): string {
  switch (status) {
    case "completed":
      return "✓";
    case "in_progress":
      return "●";
    case "skipped":
      return "–";
    case "failed":
      return "×";
    default:
      return "○";
  }
}

export function isChecklistVisible(checklist: SessionChecklist | null | undefined): checklist is SessionChecklist {
  if (!checklist) return false;
  if (checklist.state === "stale") return false;
  return Array.isArray(checklist.items) && checklist.items.length > 0;
}

export function SessionChecklistStrip({
  checklist,
  sessionId,
}: {
  checklist: SessionChecklist | null | undefined;
  sessionId: string;
}) {
  const [expanded, setExpanded] = useState<boolean>(() => expandedBySession.get(sessionId) ?? false);
  const listId = useId();

  useEffect(() => {
    setExpanded(expandedBySession.get(sessionId) ?? false);
  }, [sessionId]);

  if (!isChecklistVisible(checklist)) {
    return null;
  }

  const total = checklist.counts?.total ?? checklist.items.length;
  const done = checklist.counts?.done ?? checklist.items.filter((i) => normalizeStatus(i.status) === "completed").length;
  const isComplete = checklist.state === "complete";
  const countLabel = `${done}/${total} ${total === 1 ? "step" : "steps"}`;
  const detailLabel = isComplete
    ? "done"
    : (checklist.current_title?.trim() || null);

  const toggle = () => {
    setExpanded((prev) => {
      const next = !prev;
      expandedBySession.set(sessionId, next);
      return next;
    });
  };

  let lastGroup: string | null = null;

  return (
    <div
      className={`session-checklist-strip${expanded ? " is-expanded" : ""}${isComplete ? " is-complete" : ""}`}
      data-testid="session-checklist-strip"
    >
      <button
        type="button"
        className="session-checklist-strip__summary"
        data-testid="session-checklist-toggle"
        aria-expanded={expanded}
        aria-controls={listId}
        onClick={toggle}
      >
        <span className={`session-checklist-strip__chev${expanded ? " is-open" : ""}`} aria-hidden="true">
          ›
        </span>
        <span
          className={`session-checklist-strip__dot session-checklist-strip__dot--${isComplete ? "complete" : "active"}`}
          aria-hidden="true"
        >
          {isComplete ? "✓" : "●"}
        </span>
        <span className="session-checklist-strip__count">{countLabel}</span>
        {detailLabel ? (
          <span className="session-checklist-strip__current" title={detailLabel}>
            · {detailLabel}
          </span>
        ) : null}
      </button>

      {expanded ? (
        <ul id={listId} className="session-checklist-strip__list" data-testid="session-checklist-list">
          {checklist.explanation ? (
            <li className="session-checklist-strip__note">{checklist.explanation}</li>
          ) : null}
          {checklist.items.map((item: SessionChecklistItem, idx: number) => {
            const norm = normalizeStatus(item.status);
            const group = item.group?.trim() || null;
            const showGroup = group !== null && group !== lastGroup;
            if (group !== null) lastGroup = group;
            const label =
              norm === "in_progress" && item.active_title?.trim()
                ? item.active_title.trim()
                : item.title;
            return (
              <li key={item.id ?? `${idx}:${item.title}`} className="session-checklist-strip__entry">
                {showGroup ? (
                  <div className="session-checklist-strip__group">{group}</div>
                ) : null}
                <div className={`session-checklist-strip__row session-checklist-strip__row--${norm}`}>
                  <span
                    className={`session-checklist-strip__glyph session-checklist-strip__glyph--${norm}`}
                    aria-hidden="true"
                  >
                    {statusGlyph(norm)}
                  </span>
                  <span className="session-checklist-strip__title">{label}</span>
                </div>
              </li>
            );
          })}
          {checklist.truncated ? (
            <li className="session-checklist-strip__note">Additional steps truncated</li>
          ) : null}
        </ul>
      ) : null}
    </div>
  );
}
