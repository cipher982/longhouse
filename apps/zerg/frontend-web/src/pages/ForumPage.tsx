import { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Badge, Button, Card, PageShell, SectionHeader, Spinner } from "../components/ui";
import { PresenceBadge, PresenceHero } from "../components/PresenceBadge";
import { SessionChat } from "../components/SessionChat";
import { ForumCanvas } from "../forum/ForumCanvas";
import { useActiveSessions } from "../hooks/useActiveSessions";
import {
  buildForumStateFromSessions,
  getSessionDisplayTitle,
  getSessionRoomLabel,
} from "../forum/session-mapper";
import { parseUTC } from "../lib/dateUtils";
import "../styles/forum.css";

function formatRelativeTime(timestamp: string): string {
  const ts = parseUTC(timestamp).getTime();
  if (!Number.isFinite(ts)) return "unknown";
  const diffMs = Date.now() - ts;
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function formatDuration(startedAt: string, endedAt: string | null): string {
  const start = parseUTC(startedAt).getTime();
  if (!Number.isFinite(start)) return "unknown";
  const end = endedAt ? parseUTC(endedAt).getTime() : Date.now();
  const diffMs = end - start;
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return "< 1m";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remMins = minutes % 60;
  if (remMins === 0) return `${hours}h`;
  return `${hours}h ${remMins}m`;
}

function sessionSortKey(status: string): number {
  if (status === "working") return 0;
  if (status === "idle") return 1;
  return 2; // completed and everything else
}

export default function ForumPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const params = new URLSearchParams(location.search);
  const sessionParam = params.get("session");
  const chatParam = params.get("chat") === "true";

  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [focusEntityId, setFocusEntityId] = useState<string | null>(null);
  const [chatMode, setChatMode] = useState(false);

  const { data: sessionsData, isLoading: sessionsLoading, error: sessionsError } = useActiveSessions({
    pollInterval: 2000,
    limit: 50,
    days_back: 7,
  });

  const isAuthError = (sessionsError as { status?: number } | null)?.status === 401;

  const sessions = useMemo(() => {
    const list = sessionsData?.sessions ?? [];
    return [...list].sort((a, b) => {
      const groupDiff = sessionSortKey(a.status) - sessionSortKey(b.status);
      if (groupDiff !== 0) return groupDiff;
      return parseUTC(b.last_activity_at).getTime() - parseUTC(a.last_activity_at).getTime();
    });
  }, [sessionsData]);

  const activeCount = useMemo(
    () => sessions.filter(s => s.status === "working" || s.presence_state === "thinking" || s.presence_state === "running").length,
    [sessions]
  );

  const canvasState = useMemo(() => buildForumStateFromSessions(sessions), [sessions]);

  const selectedSession = selectedSessionId
    ? sessions.find((session) => session.id === selectedSessionId)
    : null;

  useEffect(() => {
    if (selectedSessionId && !selectedSession) {
      setSelectedSessionId(null);
      setFocusEntityId(null);
    }
  }, [selectedSessionId, selectedSession]);

  useEffect(() => {
    setChatMode(false);
  }, [selectedSessionId]);

  useEffect(() => {
    if (!sessionParam || sessions.length === 0) return;

    if (sessions.some((session) => session.id === sessionParam)) {
      setSelectedSessionId(sessionParam);
      setFocusEntityId(sessionParam);
      if (chatParam) {
        setChatMode(true);
      }
      navigate("/forum", { replace: true });
    }
  }, [sessionParam, chatParam, sessions, navigate]);

  const handleFocus = () => {
    if (!selectedSessionId) return;
    setFocusEntityId((prev) => (prev === selectedSessionId ? null : selectedSessionId));
  };

  return (
    <PageShell size="full" className="forum-map-page">
      <SectionHeader
        title="The Forum"
        description="Live session desks across your repos"
        actions={
          <div className="forum-map-actions">
            <Button
              variant="secondary"
              size="sm"
              onClick={() =>
                queryClient.invalidateQueries({ queryKey: ["active-sessions"], exact: false })
              }
            >
              Refresh
            </Button>
            <Button variant="ghost" size="sm" onClick={() => navigate("/runs")}>
              Runs
            </Button>
          </div>
        }
      />

      <div className="forum-map-grid">
        <Card className="forum-map-panel forum-map-panel--left">
          <div className="forum-panel-header">
            <div>
              <div className="forum-panel-title">Sessions</div>
              <div className="forum-panel-subtitle">{sessions.length} total</div>
            </div>
            {activeCount > 0 ? (
              <span className="forum-active-count">
                <span className="forum-active-count-dot" />
                {activeCount} live
              </span>
            ) : (
              <Badge variant="neutral">Idle</Badge>
            )}
          </div>
          <div className="forum-session-list">
            {sessions.length === 0 ? (
              <div className="forum-task-empty">
                {sessionsLoading ? "Loading sessions..." : "No sessions found in the last 7 days."}
              </div>
            ) : (
              sessions.map((session) => {
                const isActive =
                  session.status === "working" ||
                  session.presence_state === "thinking" ||
                  session.presence_state === "running";
                const isInactive = !isActive && (session.status === "completed" || session.ended_at != null || session.status === "idle");
                const rowClass = [
                  "forum-session-row",
                  session.id === selectedSessionId ? "forum-session-row--selected" : "",
                  isActive ? "forum-session-row--active" : "",
                  isInactive ? "forum-session-row--inactive" : "",
                ]
                  .filter(Boolean)
                  .join(" ");

                return (
                  <button
                    key={session.id}
                    className={rowClass}
                    type="button"
                    onClick={() => {
                      setSelectedSessionId(session.id);
                    }}
                  >
                    <div className="forum-session-title">
                      {isActive && <span className="forum-session-active-dot" />}
                      {getSessionDisplayTitle(session)}
                    </div>
                    <div className="forum-session-meta">
                      {getSessionRoomLabel(session)} | {session.provider} |{" "}
                      {formatRelativeTime(session.last_activity_at)}
                    </div>
                    <div style={{ marginTop: 4 }}>
                      <PresenceBadge
                        state={session.presence_state}
                        tool={session.presence_tool}
                        heuristicActive={session.status === "working" && session.ended_at == null}
                      />
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </Card>

        <Card className="forum-map-panel forum-map-panel--center">
          {isAuthError ? (
            <div className="forum-canvas-loading">
              <span style={{ color: "var(--color-intent-warning, #f59e0b)", fontSize: "1.5rem" }}>⚠</span>
              <span>Session expired.</span>
              <Button variant="primary" size="sm" onClick={() => window.location.reload()}>
                Refresh to log in
              </Button>
            </div>
          ) : sessionsLoading ? (
            <div className="forum-canvas-loading">
              <Spinner size="lg" />
              <span>Loading sessions...</span>
            </div>
          ) : (
            <ForumCanvas
              state={canvasState}
              selectedEntityId={selectedSessionId}
              focusEntityId={focusEntityId}
              onSelectEntity={setSelectedSessionId}
            />
          )}
        </Card>

        <Card className="forum-map-panel forum-map-panel--right">
          {chatMode && selectedSession && selectedSession.provider === "claude" ? (
            <SessionChat session={selectedSession} onClose={() => setChatMode(false)} />
          ) : (
            <>
              <div className="forum-panel-header">
                <div>
                  <div className="forum-panel-title">Drop-In</div>
                  <div className="forum-panel-subtitle">Selection details</div>
                </div>
                {selectedSession ? (
                  <Badge variant="success">Selected</Badge>
                ) : (
                  <Badge variant="neutral">Idle</Badge>
                )}
              </div>
              <div className="forum-selection">
                {selectedSession ? (
                  <>
                    <PresenceHero
                      state={selectedSession.presence_state}
                      tool={selectedSession.presence_tool}
                    />
                    <div className="forum-selection-title">
                      {getSessionDisplayTitle(selectedSession)}
                    </div>
                    <div className="forum-selection-pills">
                      <span className={`forum-duration-pill${selectedSession.ended_at == null && (selectedSession.status === "working" || selectedSession.presence_state != null) ? " forum-duration-pill--active" : ""}`}>
                        {formatDuration(selectedSession.started_at, selectedSession.ended_at)}
                      </span>
                      <span className="forum-turns-pill">
                        {selectedSession.message_count} turns · {selectedSession.tool_calls} tools
                      </span>
                    </div>
                    {selectedSession.last_assistant_message && (
                      <div className="forum-selection-preview">
                        <div className="forum-selection-preview-label">Last message</div>
                        <div className="forum-selection-preview-text forum-selection-preview-text--terminal">
                          {selectedSession.last_assistant_message}
                        </div>
                      </div>
                    )}
                    <div className="forum-selection-divider" />
                    <div className="forum-selection-meta">
                      <span className="forum-selection-meta-label">Provider</span> {selectedSession.provider}
                    </div>
                    {selectedSession.git_branch && (
                      <div className="forum-selection-meta">
                        <span className="forum-selection-meta-label">Branch</span> {selectedSession.git_branch}
                      </div>
                    )}
                    {selectedSession.cwd && (
                      <div className="forum-selection-meta">
                        <span className="forum-selection-meta-label">CWD</span> {selectedSession.cwd}
                      </div>
                    )}
                    <div className="forum-selection-meta forum-selection-meta--time">
                      {formatRelativeTime(selectedSession.last_activity_at)}
                    </div>
                    <div className="forum-selection-actions">
                      <Button size="sm" variant="primary" onClick={handleFocus}>
                        {focusEntityId === selectedSession.id ? "Unfocus" : "Focus"}
                      </Button>
                      {selectedSession.provider === "claude" && (
                        <Button size="sm" variant="secondary" onClick={() => setChatMode(true)}>
                          Chat
                        </Button>
                      )}
                    </div>
                  </>
                ) : (
                  <div className="forum-selection-empty">Select a session desk to inspect.</div>
                )}
              </div>
              <div className="forum-stats-bar">
                <span className="forum-stats-active">
                  <span className="forum-stats-dot" />
                  {sessions.filter(s => s.status === "working" || s.presence_state === "thinking" || s.presence_state === "running").length} active
                </span>
                <span className="forum-stats-sep">·</span>
                <span className="forum-stats-idle">
                  {sessions.filter(s => s.status === "idle").length} idle
                </span>
                <span className="forum-stats-sep">·</span>
                <span className="forum-stats-total">
                  {sessions.length} total
                </span>
              </div>
            </>
          )}
        </Card>
      </div>
    </PageShell>
  );
}
