import { Fragment, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent } from "react";
import { PlayIcon, MessageCircleIcon, SettingsIcon, TrashIcon, ChevronRightIcon, ChevronDownIcon } from "../../components/icons";
import type { AgentSummary, AgentRun } from "../../services/api";
import { formatDateTimeShort, formatStatus, formatDuration, capitaliseFirst, formatTokens, formatCost, formatRunStatusIcon } from "./formatters";
import { computeSuccessStats, determineLastRunIndicator } from "./sorting";

interface AgentTableRowProps {
  agent: AgentSummary;
  runs: AgentRun[];
  includeOwner: boolean;
  isExpanded: boolean;
  isRunHistoryExpanded: boolean;
  isPendingRun: boolean;
  runsDataLoading: boolean;
  editingAgentId: number | null;
  editingName: string;
  onToggleRow: (agentId: number) => void;
  onToggleRunHistory: (agentId: number) => void;
  onRunAgent: (event: ReactMouseEvent<HTMLButtonElement>, agentId: number, status: string) => void;
  onChatAgent: (event: ReactMouseEvent<HTMLButtonElement>, agentId: number, agentName: string) => void;
  onDebugAgent: (event: ReactMouseEvent<HTMLButtonElement>, agentId: number) => void;
  onDeleteAgent: (event: ReactMouseEvent<HTMLButtonElement>, agentId: number, name: string) => void;
  onStartEditingName: (agentId: number, currentName: string) => void;
  onSaveNameAndExit: (agentId: number) => void;
  onCancelEditing: () => void;
  onEditingNameChange: (name: string) => void;
  onRunActionsClick: (agentId: number, runId: number) => void;
}

export function AgentTableRow({
  agent,
  runs,
  includeOwner,
  isExpanded,
  isRunHistoryExpanded,
  isPendingRun,
  runsDataLoading,
  editingAgentId,
  editingName,
  onToggleRow,
  onToggleRunHistory,
  onRunAgent,
  onChatAgent,
  onDebugAgent,
  onDeleteAgent,
  onStartEditingName,
  onSaveNameAndExit,
  onCancelEditing,
  onEditingNameChange,
  onRunActionsClick,
}: AgentTableRowProps) {
  const successStats = computeSuccessStats(runs);
  const lastRunIndicator = determineLastRunIndicator(runs);
  const isRunning = agent.status === "running";
  const createdDisplay = formatDateTimeShort(agent.created_at ?? null);
  const lastRunDisplay = formatDateTimeShort(agent.last_run_at ?? null);
  const nextRunDisplay = formatDateTimeShort(agent.next_run_at ?? null);
  const emptyColspan = includeOwner ? 8 : 7;

  const handleRowKeyDown = (event: ReactKeyboardEvent<HTMLTableRowElement>) => {
    const key = event.key;
    if (key === "Enter") {
      event.preventDefault();
      onToggleRow(agent.id);
      return;
    }

    if (key !== "ArrowDown" && key !== "ArrowUp") {
      return;
    }

    event.preventDefault();
    const current = event.currentTarget;
    const tbody = current.closest("tbody");
    if (!tbody) {
      return;
    }
    const rows = Array.from(tbody.querySelectorAll<HTMLTableRowElement>("tr[data-agent-id]"));
    const index = rows.indexOf(current);
    if (index === -1) {
      return;
    }
    const nextIndex = key === "ArrowDown" ? Math.min(rows.length - 1, index + 1) : Math.max(0, index - 1);
    rows[nextIndex]?.focus();
  };

  const detailRowId = `agent-detail-${agent.id}`;

  return (
    <Fragment key={agent.id}>
      <tr
        data-agent-id={agent.id}
        className={`agent-row ${agent.status === "error" ? "error-row" : ""}`}
        tabIndex={0}
        onClick={() => onToggleRow(agent.id)}
        onKeyDown={handleRowKeyDown}
      >
        <td data-label="Name" className="name-cell">
          <button
            type="button"
            className="expand-toggle-btn"
            aria-expanded={isExpanded}
            aria-controls={detailRowId}
            aria-label={isExpanded ? `Collapse ${agent.name} details` : `Expand ${agent.name} details`}
            onClick={(e) => {
              e.stopPropagation();
              onToggleRow(agent.id);
            }}
          >
            {isExpanded ? <ChevronDownIcon /> : <ChevronRightIcon />}
          </button>
          {editingAgentId === agent.id ? (
            <input
              className="inline-edit-input"
              value={editingName}
              onChange={(e) => onEditingNameChange(e.target.value)}
              onBlur={() => onSaveNameAndExit(agent.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.stopPropagation();
                  onSaveNameAndExit(agent.id);
                }
                if (e.key === "Escape") {
                  e.stopPropagation();
                  onCancelEditing();
                }
              }}
              onClick={(e) => e.stopPropagation()}
              onMouseDown={(e) => e.stopPropagation()}
              autoFocus
            />
          ) : (
            <span
              className="editable-name"
              onClick={() => onStartEditingName(agent.id, agent.name)}
              title="Click to rename"
            >
              {agent.name}
            </span>
          )}
        </td>
        {includeOwner && (
          <td className="owner-cell" data-label="Owner">
            {renderOwnerCell(agent)}
          </td>
        )}
        <td data-label="Status">
          <span className={`status-indicator status-${agent.status.toLowerCase()}`}>
            {formatStatus(agent.status)}
          </span>
          {agent.last_error && agent.last_error.trim() && (
            <span className="info-icon" title={agent.last_error}>
              ℹ
            </span>
          )}
          {lastRunIndicator !== null && (
            <span
              className={lastRunIndicator ? "last-run-indicator last-run-success" : "last-run-indicator last-run-failure"}
            >
              {lastRunIndicator ? " (Last: ✓)" : " (Last: ✗)"}
            </span>
          )}
        </td>
        <td data-label="Created">{createdDisplay}</td>
        <td data-label="Last Run">{lastRunDisplay}</td>
        <td data-label="Next Run">{nextRunDisplay}</td>
        <td data-label="Success Rate">{successStats.display}</td>
        <td className="actions-cell" data-label="Actions">
          <div className="actions-cell-inner">
            <button
              type="button"
              className={`action-btn run-btn${isRunning || isPendingRun ? " disabled" : ""}`}
              data-testid={`run-agent-${agent.id}`}
              disabled={isRunning || isPendingRun}
              title={isRunning ? "Agent is already running" : "Run Agent"}
              aria-label={isRunning ? "Agent is already running" : "Run Agent"}
              onClick={(event) => onRunAgent(event, agent.id, agent.status)}
            >
              <PlayIcon />
            </button>
            <button
              type="button"
              className="action-btn chat-btn"
              data-testid={`chat-agent-${agent.id}`}
              title="Chat with Agent"
              aria-label="Chat with Agent"
              onClick={(event) => onChatAgent(event, agent.id, agent.name)}
            >
              <MessageCircleIcon />
            </button>
            <button
              type="button"
              className="action-btn debug-btn"
              data-testid={`debug-agent-${agent.id}`}
              title="Debug / Info"
              aria-label="Debug / Info"
              onClick={(event) => onDebugAgent(event, agent.id)}
            >
              <SettingsIcon />
            </button>
            <button
              type="button"
              className="action-btn delete-btn"
              data-testid={`delete-agent-${agent.id}`}
              title="Delete Agent"
              aria-label="Delete Agent"
              onClick={(event) => onDeleteAgent(event, agent.id, agent.name)}
            >
              <TrashIcon />
            </button>
          </div>
        </td>
      </tr>
      {isExpanded && (
        <tr id={detailRowId} className="agent-detail-row" key={`detail-${agent.id}`}>
          <td colSpan={emptyColspan}>
            <div className="agent-detail-container">
              {runsDataLoading && <span>Loading run history...</span>}
              {!runsDataLoading && runs && runs.length === 0 && (
                <span>No runs recorded yet.</span>
              )}
              {!runsDataLoading && runs && runs.length > 0 && (
                <>
                  <table className="run-history-table">
                    <thead>
                      <tr>
                        <th>Status</th>
                        <th>Started</th>
                        <th>Duration</th>
                        <th>Trigger</th>
                        <th>Tokens</th>
                        <th>Cost</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {runs
                        .slice(0, isRunHistoryExpanded ? runs.length : Math.min(runs.length, 5))
                        .map((run) => (
                          <tr key={run.id}>
                            <td>{formatRunStatusIcon(run.status)}</td>
                            <td>{formatDateTimeShort(run.started_at ?? null)}</td>
                            <td>{formatDuration(run.duration_ms)}</td>
                            <td>{capitaliseFirst(run.trigger)}</td>
                            <td>{formatTokens(run.total_tokens)}</td>
                            <td>{formatCost(run.total_cost_usd)}</td>
                            <td className="run-kebab-cell">
                              <span
                                className="kebab-menu-btn"
                                role="button"
                                tabIndex={0}
                                onClick={(event) => {
                                  event.preventDefault();
                                  event.stopPropagation();
                                  onRunActionsClick(agent.id, run.id);
                                }}
                                onKeyDown={(event) => {
                                  if (event.key === "Enter" || event.key === " ") {
                                    event.preventDefault();
                                    event.stopPropagation();
                                    onRunActionsClick(agent.id, run.id);
                                  }
                                }}
                              >
                                ⋮
                              </span>
                            </td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                  {runs.length > 5 && (
                    <a
                      href="#"
                      className="run-toggle-link"
                      aria-expanded={isRunHistoryExpanded ? "true" : "false"}
                      onClick={(event) => {
                        event.preventDefault();
                        onToggleRunHistory(agent.id);
                      }}
                    >
                      {isRunHistoryExpanded ? "Show less" : `Show all (${runs.length})`}
                    </a>
                  )}
                </>
              )}
            </div>
          </td>
        </tr>
      )}
    </Fragment>
  );
}

function renderOwnerCell(agent: AgentSummary) {
  if (!agent.owner) {
    return <span>-</span>;
  }

  const label = agent.owner.display_name?.trim() || agent.owner.email;
  if (!label) {
    return <span>-</span>;
  }

  return (
    <div className="owner-wrapper">
      {agent.owner.avatar_url && <img src={agent.owner.avatar_url} alt="" className="owner-avatar" aria-hidden="true" />}
      <span>{label}</span>
    </div>
  );
}
