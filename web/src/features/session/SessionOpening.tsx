import { useMemo } from "react";
import { createPortal } from "react-dom";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import type { AgentSession, TimelineSessionsListResponse } from "@/shared/api/agents";
import { getProviderLabel } from "@/shared/lib/providers";
import { getSessionCardText } from "@/shared/session/sessionLabels";
import { Button } from "@/shared/ui";
import { ProviderGlyph } from "@/shared/ui/ProviderGlyph";
import { buildSessionMetaItems } from "./sessionHeaderState";
import "./session-opening.css";

/** The session as the Timeline (or the rail) last listed it, if it is cached. */
export function findListedSession(queryClient: QueryClient, sessionId: string | null): AgentSession | null {
  if (!sessionId) return null;
  for (const [, data] of queryClient.getQueriesData<TimelineSessionsListResponse>({ queryKey: ["agent-sessions"] })) {
    const card = data?.sessions?.find((c) => c.head?.id === sessionId);
    if (card) return card.head;
  }
  return null;
}

// Widths vary so the placeholder reads as text, not as a table.
const SKELETON_ROWS = ["62%", "88%", "45%", "93%", "71%", "38%", "84%", "57%"];

/**
 * What a session shows in the frame after the click, before its transcript
 * arrives: the title bar from the Timeline's own data (when it is cached) and
 * placeholder rows where the transcript will land. Nothing here is a control
 * the session may not have; the real page replaces it as soon as the
 * workspace answers.
 */
export function SessionOpening({
  sessionId,
  headerTarget,
  onBack,
}: {
  sessionId: string | null;
  headerTarget: HTMLElement | null;
  onBack: () => void;
}) {
  const queryClient = useQueryClient();
  const listed = useMemo(() => findListedSession(queryClient, sessionId), [queryClient, sessionId]);
  const title = listed ? getSessionCardText(listed, { titleMaxChars: 96 }).title : null;
  const meta = listed
    ? buildSessionMetaItems({
        provider: listed.provider ? getProviderLabel(listed.provider) : null,
        project: listed.project?.trim() || null,
        // Same order the loaded page uses, so the line does not change on load.
        host: listed.control?.source_runner_name?.trim() || listed.device_id?.trim() || null,
        messages: (listed.user_messages ?? 0) + (listed.assistant_messages ?? 0),
        toolCalls: listed.tool_calls ?? 0,
      }).join(" · ")
    : "";

  const headerBar = (
    <div
      className={`timeline-pane__header timeline-header${headerTarget ? " timeline-pane__header--app-bar" : ""}`}
      data-testid="session-opening-header"
    >
      <div className="timeline-pane__header-main">
        <div className="session-workspace-header__left">
          <Button variant="ghost" size="sm" onClick={onBack} title="Back to timeline" aria-label="Back to timeline">
            &larr;
          </Button>
          <div className="session-workspace-header__title-stack">
            {title ? (
              <span className="session-workspace-header__name" title={title}>
                {title}
              </span>
            ) : (
              <span className="session-opening__bar session-opening__bar--title" aria-hidden="true" />
            )}
            <span className="session-workspace-header__identity">
              {listed?.provider ? (
                <ProviderGlyph
                  provider={listed.provider}
                  size={13}
                  variant="bare"
                  className="session-workspace-header__provider-glyph"
                />
              ) : null}
              <span className="session-workspace-header__meta-text">{meta || "Opening session…"}</span>
            </span>
          </div>
        </div>
      </div>
    </div>
  );

  return (
    <div className="session-workspace-route session-opening" data-testid="session-opening" aria-busy="true">
      {headerTarget ? createPortal(headerBar, headerTarget) : headerBar}
      <div className="session-opening__rows" role="status" aria-label="Loading the conversation">
        {SKELETON_ROWS.map((width, i) => (
          <span key={i} className="session-opening__bar" style={{ width }} />
        ))}
      </div>
    </div>
  );
}
