/**
 * SessionPickerModal - Browse and select past AI sessions for resumption
 *
 * Features:
 * - Search sessions by content
 * - Filter by project and provider
 * - Preview session messages
 * - Keyboard navigation (Up/Down/Enter/Esc)
 */

import React, { useState, useCallback, useRef, useEffect, useMemo } from "react";
import { Button } from "./ui";
import { useLifeHubSessions, useSessionPreview } from "../hooks/useLifeHubSessions";
import type { SessionSummary, SessionFilters, SessionMessage } from "../services/api";
import "./SessionPickerModal.css";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface SessionPickerModalProps {
  isOpen: boolean;
  initialFilters?: SessionFilters;
  onClose: () => void;
  onSelect: (sessionId: string) => void;
  onStartNew?: () => void;
}

// ---------------------------------------------------------------------------
// Helper Components
// ---------------------------------------------------------------------------

function formatRelativeTime(dateStr: string): string {
  const date = new Date(dateStr);
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

function getProviderIcon(provider: string): string {
  switch (provider) {
    case "claude": return "C";
    case "codex": return "X";
    case "gemini": return "G";
    default: return "?";
  }
}

function truncatePath(path: string | null, maxLen: number = 30): string {
  if (!path) return "";
  if (path.length <= maxLen) return path;
  const parts = path.split("/");
  if (parts.length <= 2) return "..." + path.slice(-maxLen);
  return "~/" + parts.slice(-2).join("/");
}

// ---------------------------------------------------------------------------
// Session List Item
// ---------------------------------------------------------------------------

interface SessionItemProps {
  session: SessionSummary;
  isSelected: boolean;
  onClick: () => void;
}

function SessionItem({ session, isSelected, onClick }: SessionItemProps) {
  // Use last user message as title, or fallback to project/cwd
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
        <span className={`provider-badge provider-${session.provider}`}>
          {getProviderIcon(session.provider)}
        </span>
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

// ---------------------------------------------------------------------------
// Session Preview Panel
// ---------------------------------------------------------------------------

interface PreviewPanelProps {
  sessionId: string | null;
}

function PreviewPanel({ sessionId }: PreviewPanelProps) {
  const { data, isLoading, error } = useSessionPreview(sessionId);

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
        {data.messages.map((msg: SessionMessage, idx: number) => (
          <div key={idx} className={`preview-message preview-${msg.role}`}>
            <span className="preview-role">{msg.role === "user" ? "You" : "AI"}</span>
            <span className="preview-content">{msg.content}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Filter Dropdown
// ---------------------------------------------------------------------------

const PROVIDERS = ["claude", "codex", "gemini"] as const;
const PROJECTS = ["zerg", "life-hub", "sauron", "hdr", "mytech"] as const;

interface FilterSelectProps {
  label: string;
  value: string;
  options: readonly string[];
  onChange: (value: string) => void;
}

function FilterSelect({ label, value, options, onChange }: FilterSelectProps) {
  return (
    <select
      className="filter-select"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={label}
    >
      <option value="">All {label}s</option>
      {options.map((opt) => (
        <option key={opt} value={opt}>
          {opt}
        </option>
      ))}
    </select>
  );
}

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

export function SessionPickerModal({
  isOpen,
  initialFilters = {},
  onClose,
  onSelect,
  onStartNew,
}: SessionPickerModalProps) {
  // Search and filter state
  const [searchQuery, setSearchQuery] = useState(initialFilters.query || "");
  const [debouncedQuery, setDebouncedQuery] = useState(searchQuery);
  const [project, setProject] = useState(initialFilters.project || "");
  const [provider, setProvider] = useState(initialFilters.provider || "");

  // Selection state
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);

  // Refs
  const searchInputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Debounce search query
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedQuery(searchQuery);
    }, 300);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  // Build filters
  const filters: SessionFilters = useMemo(() => ({
    query: debouncedQuery || undefined,
    project: project || undefined,
    provider: provider || undefined,
    days_back: 30,
    limit: 50,
  }), [debouncedQuery, project, provider]);

  // Fetch sessions
  const { data, isLoading, error } = useLifeHubSessions(filters, { enabled: isOpen });

  const sessions: SessionSummary[] = data?.sessions || [];

  // Update selection when sessions change
  useEffect(() => {
    if (sessions.length > 0) {
      setSelectedIndex(0);
      setSelectedSessionId(sessions[0].id);
    } else {
      setSelectedIndex(-1);
      setSelectedSessionId(null);
    }
  }, [sessions]);

  // Focus search input when modal opens
  useEffect(() => {
    if (isOpen) {
      setTimeout(() => searchInputRef.current?.focus(), 100);
    }
  }, [isOpen]);

  // Keyboard navigation
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          if (sessions.length > 0) {
            const nextIndex = Math.min(selectedIndex + 1, sessions.length - 1);
            setSelectedIndex(nextIndex);
            setSelectedSessionId(sessions[nextIndex].id);
            // Scroll into view
            const item = listRef.current?.children[nextIndex] as HTMLElement;
            item?.scrollIntoView({ block: "nearest" });
          }
          break;
        case "ArrowUp":
          e.preventDefault();
          if (sessions.length > 0) {
            const prevIndex = Math.max(selectedIndex - 1, 0);
            setSelectedIndex(prevIndex);
            setSelectedSessionId(sessions[prevIndex].id);
            const item = listRef.current?.children[prevIndex] as HTMLElement;
            item?.scrollIntoView({ block: "nearest" });
          }
          break;
        case "Enter":
          e.preventDefault();
          if (selectedSessionId) {
            onSelect(selectedSessionId);
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
    [sessions, selectedIndex, selectedSessionId, onSelect, onClose]
  );

  // Handle session click
  const handleSessionClick = useCallback(
    (session: SessionSummary, index: number) => {
      setSelectedIndex(index);
      setSelectedSessionId(session.id);
    },
    []
  );

  // Handle resume click
  const handleResume = useCallback(() => {
    if (selectedSessionId) {
      onSelect(selectedSessionId);
    }
  }, [selectedSessionId, onSelect]);

  // Reset state when modal closes
  useEffect(() => {
    if (!isOpen) {
      setSearchQuery(initialFilters.query || "");
      setProject(initialFilters.project || "");
      setProvider(initialFilters.provider || "");
      setSelectedIndex(0);
      setSelectedSessionId(null);
    }
  }, [isOpen, initialFilters]);

  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={onClose} onKeyDown={handleKeyDown}>
      <div
        className="modal-container session-picker-modal"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Resume Session"
      >
        {/* Header */}
        <div className="modal-header">
          <h2>Resume Session</h2>
          <button
            className="modal-close-button"
            onClick={onClose}
            aria-label="Close"
          >
            &times;
          </button>
        </div>

        {/* Content */}
        <div className="modal-content session-picker-content">
          {/* Search and Filters */}
          <div className="session-picker-filters">
            <div className="search-input-wrapper">
              <input
                ref={searchInputRef}
                type="text"
                placeholder="Search sessions..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="form-input session-search-input"
              />
              <span className="search-hint">Press / to focus</span>
            </div>
            <div className="filter-row">
              <FilterSelect
                label="project"
                value={project}
                options={PROJECTS}
                onChange={setProject}
              />
              <FilterSelect
                label="provider"
                value={provider}
                options={PROVIDERS}
                onChange={setProvider}
              />
            </div>
          </div>

          {/* Main content area */}
          <div className="session-picker-body">
            {/* Session List */}
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
                <div
                  className="session-list"
                  role="listbox"
                  ref={listRef}
                >
                  {sessions.map((session, idx) => (
                    <SessionItem
                      key={session.id}
                      session={session}
                      isSelected={idx === selectedIndex}
                      onClick={() => handleSessionClick(session, idx)}
                    />
                  ))}
                </div>
              )}
            </div>

            {/* Preview Panel */}
            <PreviewPanel sessionId={selectedSessionId} />
          </div>
        </div>

        {/* Footer */}
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
            disabled={!selectedSessionId}
          >
            Resume
          </Button>
        </div>
      </div>
    </div>
  );
}

export default SessionPickerModal;
