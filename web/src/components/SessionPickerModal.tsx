/**
 * SessionPickerModal - Browse and select past AI sessions for resumption
 *
 * Features:
 * - Search sessions by content
 * - Filter by project and provider
 * - Preview session messages
 * - Keyboard navigation (Up/Down/Enter/Esc)
 */

import React, { useCallback, useMemo, useRef, useState } from "react";
import { ProviderGlyph } from "./ProviderGlyph";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { Button } from "./ui";
import {
  useAgentSessionSummaries,
  useAgentSessionPreview,
  useAgentSessionFilters,
} from "../hooks/useAgentSessions";
import type {
  AgentSessionSummary,
  AgentSessionSummaryFilters,
  AgentSessionPreviewMessage,
} from "../services/api";
import { parseUTC } from "../lib/dateUtils";
import "./SessionPickerModal.css";

interface SessionPickerModalProps {
  isOpen: boolean;
  initialFilters?: AgentSessionSummaryFilters;
  onClose: () => void;
  onSelect: (sessionId: string) => void;
  onStartNew?: () => void;
}

interface NormalizedFilters {
  query: string;
  project: string;
  provider: string;
}

function normalizeFilters(initialFilters?: AgentSessionSummaryFilters | null): NormalizedFilters {
  return {
    query: initialFilters?.query || "",
    project: initialFilters?.project || "",
    provider: initialFilters?.provider || "",
  };
}

function formatRelativeTime(dateStr: string): string {
  const date = parseUTC(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) return "Just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString();
}

function truncatePath(path: string | null, maxLen: number = 30): string {
  if (!path) return "";
  if (path.length <= maxLen) return path;
  const parts = path.split("/");
  if (parts.length <= 2) return "..." + path.slice(-maxLen);
  return "~/" + parts.slice(-2).join("/");
}

interface SessionItemProps {
  session: AgentSessionSummary;
  isSelected: boolean;
  onClick: () => void;
}

function SessionItem({ session, isSelected, onClick }: SessionItemProps) {
  const title = session.last_user_message
    ? session.last_user_message.slice(0, 60) + (session.last_user_message.length > 60 ? "..." : "")
    : session.project || truncatePath(session.cwd) || "Untitled Session";

  return (
    <div
      className={`session-item ${isSelected ? "selected" : ""}`}
      onClick={onClick}
      role="option"
      aria-selected={isSelected}
    >
      <div className="session-item-header">
        <ProviderGlyph provider={session.provider} size={20} />
        <span className="session-title">{title}</span>
      </div>
      <div className="session-item-meta">
        <span className="session-time">{formatRelativeTime(session.started_at)}</span>
        <span className="session-separator">&middot;</span>
        <span className="session-turns">{session.turn_count} turns</span>
        {session.project && (
          <>
            <span className="session-separator">&middot;</span>
            <span className="session-project">{session.project}</span>
          </>
        )}
      </div>
    </div>
  );
}

function PreviewPanel({ sessionId }: { sessionId: string | null }) {
  const { data, isLoading, error } = useAgentSessionPreview(sessionId);

  if (!sessionId) {
    return (
      <div className="preview-panel preview-empty">
        <p>Select a session to preview</p>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="preview-panel preview-loading">
        <div className="preview-skeleton">
          <div className="skeleton-line" />
          <div className="skeleton-line short" />
          <div className="skeleton-line" />
          <div className="skeleton-line short" />
        </div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="preview-panel preview-error">
        <p>Failed to load preview</p>
      </div>
    );
  }

  return (
    <div className="preview-panel">
      <div className="preview-header">
        <span>{data.total_messages} total messages</span>
      </div>
      <div className="preview-messages">
        {data.messages.map((message: AgentSessionPreviewMessage, idx: number) => (
          <div key={idx} className={`preview-message preview-${message.role}`}>
            <span className="preview-role">{message.role === "user" ? "You" : "AI"}</span>
            <span className="preview-content">{message.content}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  return (
    <select
      className="filter-select"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={label}
    >
      <option value="">All {label}s</option>
      {options.map((option) => (
        <option key={option} value={option}>
          {option}
        </option>
      ))}
    </select>
  );
}

function SessionPickerDialog({
  initialFilters,
  onClose,
  onSelect,
  onStartNew,
}: {
  initialFilters: NormalizedFilters;
  onClose: () => void;
  onSelect: (sessionId: string) => void;
  onStartNew?: () => void;
}) {
  const [searchQuery, setSearchQuery] = useState(initialFilters.query);
  const debouncedQuery = useDebouncedValue(searchQuery, 300);
  const [project, setProject] = useState(initialFilters.project);
  const [provider, setProvider] = useState(initialFilters.provider);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);

  const searchInputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const filters: AgentSessionSummaryFilters = useMemo(
    () => ({
      query: debouncedQuery || undefined,
      project: project || undefined,
      provider: provider || undefined,
      days_back: 30,
      limit: 50,
    }),
    [debouncedQuery, project, provider],
  );

  const { data, isLoading, error } = useAgentSessionSummaries(filters, { enabled: true });
  const { data: filterData } = useAgentSessionFilters(90, true);

  const sessions = useMemo<AgentSessionSummary[]>(() => data?.sessions ?? [], [data?.sessions]);
  const projectOptions = filterData?.projects ?? [];
  const providerOptions = filterData?.providers ?? [];
  const effectiveSelectedSessionId =
    selectedSessionId && sessions.some((session) => session.id === selectedSessionId)
      ? selectedSessionId
      : sessions[0]?.id ?? null;
  const selectedIndex =
    effectiveSelectedSessionId != null
      ? sessions.findIndex((session) => session.id === effectiveSelectedSessionId)
      : -1;

  const moveSelection = useCallback(
    (direction: -1 | 1) => {
      if (sessions.length === 0) return;
      const baseIndex = selectedIndex >= 0 ? selectedIndex : 0;
      const nextIndex = Math.min(Math.max(baseIndex + direction, 0), sessions.length - 1);
      const nextSession = sessions[nextIndex];
      if (!nextSession) return;
      setSelectedSessionId(nextSession.id);
      const item = listRef.current?.children[nextIndex] as HTMLElement | undefined;
      item?.scrollIntoView({ block: "nearest" });
    },
    [selectedIndex, sessions],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          moveSelection(1);
          break;
        case "ArrowUp":
          e.preventDefault();
          moveSelection(-1);
          break;
        case "Enter":
          e.preventDefault();
          if (effectiveSelectedSessionId) {
            onSelect(effectiveSelectedSessionId);
          }
          break;
        case "Escape":
          e.preventDefault();
          onClose();
          break;
        case "/":
          if (document.activeElement !== searchInputRef.current) {
            e.preventDefault();
            searchInputRef.current?.focus();
          }
          break;
      }
    },
    [effectiveSelectedSessionId, moveSelection, onClose, onSelect],
  );

  const handleResume = useCallback(() => {
    if (effectiveSelectedSessionId) {
      onSelect(effectiveSelectedSessionId);
    }
  }, [effectiveSelectedSessionId, onSelect]);

  return (
    <div className="modal-overlay" onClick={onClose} onKeyDown={handleKeyDown}>
      <div
        className="modal-container session-picker-modal"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Resume Session"
      >
        <div className="modal-header">
          <h2>Resume Session</h2>
          <button className="modal-close-button" onClick={onClose} aria-label="Close">
            &times;
          </button>
        </div>

        <div className="modal-content session-picker-content">
          <div className="session-picker-filters">
            <div className="search-input-wrapper">
              <input
                ref={searchInputRef}
                type="text"
                placeholder="Search sessions..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="form-input session-search-input"
                autoFocus
              />
              <span className="search-hint">Press / to focus</span>
            </div>
            <div className="filter-row">
              <FilterSelect
                label="project"
                value={project}
                options={projectOptions}
                onChange={setProject}
              />
              <FilterSelect
                label="provider"
                value={provider}
                options={providerOptions}
                onChange={setProvider}
              />
            </div>
          </div>

          <div className="session-picker-body">
            <div className="session-list-container">
              {isLoading ? (
                <div className="session-list-loading">
                  {[1, 2, 3, 4, 5].map((i) => (
                    <div key={i} className="session-skeleton">
                      <div className="skeleton-line" />
                      <div className="skeleton-line short" />
                    </div>
                  ))}
                </div>
              ) : error ? (
                <div className="session-list-error">
                  <p>Failed to load sessions</p>
                </div>
              ) : sessions.length === 0 ? (
                <div className="session-list-empty">
                  <p>No sessions found</p>
                  {searchQuery && <p className="empty-hint">Try a different search</p>}
                </div>
              ) : (
                <div className="session-list" role="listbox" ref={listRef}>
                  {sessions.map((session) => (
                    <SessionItem
                      key={session.id}
                      session={session}
                      isSelected={session.id === effectiveSelectedSessionId}
                      onClick={() => setSelectedSessionId(session.id)}
                    />
                  ))}
                </div>
              )}
            </div>

            <PreviewPanel sessionId={effectiveSelectedSessionId} />
          </div>
        </div>

        <div className="modal-actions">
          {onStartNew && (
            <Button variant="secondary" onClick={onStartNew}>
              Start New Session
            </Button>
          )}
          <div className="action-spacer" />
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={handleResume}
            disabled={!effectiveSelectedSessionId}
          >
            Resume
          </Button>
        </div>
      </div>
    </div>
  );
}

export function SessionPickerModal({
  isOpen,
  initialFilters,
  onClose,
  onSelect,
  onStartNew,
}: SessionPickerModalProps) {
  if (!isOpen) return null;

  return (
    <SessionPickerDialog
      key={JSON.stringify(normalizeFilters(initialFilters))}
      initialFilters={normalizeFilters(initialFilters)}
      onClose={onClose}
      onSelect={onSelect}
      onStartNew={onStartNew}
    />
  );
}

export default SessionPickerModal;
