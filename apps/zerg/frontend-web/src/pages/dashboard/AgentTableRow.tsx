import React, { Fragment, memo, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type ReactElement } from "react";
import clsx from "clsx";
import {
  PlayIcon,
  MessageCircleIcon,
  SettingsIcon,
  TrashIcon,
  InfoCircleIcon,
  CheckCircleIcon,
  XCircleIcon,
  CircleIcon,
  CircleDotIcon,
  LoaderIcon,
  AlertTriangleIcon,
} from "../../components/icons";
import type { AgentSummary, AgentRun } from "../../services/api";
import { Table, Badge, IconButton } from "../../components/ui";
import { formatDateTimeShort, formatDuration, capitaliseFirst, formatTokens, formatCost } from "./formatters";
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

function formatStatus(status: string): ReactElement {
  switch (status) {
    case "running":
      return <><CircleDotIcon width={12} height={12} /> Running</>;
    case "processing":
      return <><LoaderIcon width={12} height={12} /> Processing</>;
    case "error":
      return <><AlertTriangleIcon width={12} height={12} /> Error</>;
    case "idle":
    default:
      return <><CircleIcon width={12} height={12} /> Idle</>;
  }
}

function formatRunStatusIcon(status: AgentRun["status"]): ReactElement {
  switch (status) {
    case "running":
      return <PlayIcon width={12} height={12} />;
    case "deferred":
      return <LoaderIcon width={12} height={12} className="animate-spin" />;
    case "success":
      return <CheckCircleIcon width={12} height={12} />;
    case "failed":
      return <XCircleIcon width={12} height={12} />;
    default:
      return <CircleDotIcon width={12} height={12} />;
  }
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

function AgentTableRowComponent({
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

  return (
    <Fragment>
      <Table.Row
        data-agent-id={agent.id}
        aria-expanded={isExpanded}
        className={clsx('agent-row', agent.status === "error" && "error-row")}
        onClick={() => onToggleRow(agent.id)}
        onKeyDown={handleRowKeyDown}
      >
        <Table.Cell data-label="Name" className="name-cell">
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
        </Table.Cell>
        {includeOwner && (
          <Table.Cell className="owner-cell" data-label="Owner">
            {renderOwnerCell(agent)}
          </Table.Cell>
        )}
        <Table.Cell data-label="Status">
          <Badge variant={agent.status === 'error' ? 'error' : agent.status === 'running' || agent.status === 'processing' ? 'warning' : 'success'}>
            {formatStatus(agent.status)}
          </Badge>
          {agent.last_error && agent.last_error.trim() && (
            <span className="info-icon" title={agent.last_error}>
              <InfoCircleIcon width={14} height={14} />
            </span>
          )}
          {lastRunIndicator !== null && (
            <span
              className={lastRunIndicator ? "last-run-indicator last-run-success" : "last-run-indicator last-run-failure"}
            >
              {lastRunIndicator
                ? <> (Last: <CheckCircleIcon width={12} height={12} />)</>
                : <> (Last: <XCircleIcon width={12} height={12} />)</>}
            </span>
          )}
        </Table.Cell>
        <Table.Cell data-label="Created">{createdDisplay}</Table.Cell>
        <Table.Cell data-label="Last Run">{lastRunDisplay}</Table.Cell>
        <Table.Cell data-label="Next Run">{nextRunDisplay}</Table.Cell>
        <Table.Cell data-label="Success Rate">{successStats.display}</Table.Cell>
        <Table.Cell className="actions-cell" data-label="Actions">
          <div className="actions-cell-inner">
            <IconButton
              className={clsx("run-btn", (isRunning || isPendingRun) && "disabled")}
              data-testid={`run-agent-${agent.id}`}
              disabled={isRunning || isPendingRun}
              title={isRunning ? "Agent is already running" : "Run Agent"}
              onClick={(event) => onRunAgent(event, agent.id, agent.status)}
            >
              <PlayIcon />
            </IconButton>
            <IconButton
              className="chat-btn"
              data-testid={`chat-agent-${agent.id}`}
              title="Chat with Agent"
              onClick={(event) => onChatAgent(event, agent.id, agent.name)}
            >
              <MessageCircleIcon />
            </IconButton>
            <IconButton
              className="debug-btn"
              data-testid={`debug-agent-${agent.id}`}
              title="Debug / Info"
              onClick={(event) => onDebugAgent(event, agent.id)}
            >
              <SettingsIcon />
            </IconButton>
            <IconButton
              className="delete-btn"
              data-testid={`delete-agent-${agent.id}`}
              title="Delete Agent"
              onClick={(event) => onDeleteAgent(event, agent.id, agent.name)}
            >
              <TrashIcon />
            </IconButton>
          </div>
        </Table.Cell>
      </Table.Row>
      {isExpanded && (
        <tr className="agent-detail-row" key={`detail-${agent.id}`}>
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

// Wrap with React.memo for performance optimization
// The component will only re-render when its props change
export const AgentTableRow = memo(AgentTableRowComponent);
