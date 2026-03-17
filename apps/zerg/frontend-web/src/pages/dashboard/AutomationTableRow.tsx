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
import type { AutomationSummary, Run } from "../../services/api";
import { Table, Badge, IconButton } from "../../components/ui";
import { formatDateTimeShort, formatDuration, capitaliseFirst, formatTokens, formatCost } from "./formatters";
import { computeRunSuccessStats, determineLastRunIndicator } from "./sorting";

interface AutomationTableRowProps {
  automation: AutomationSummary;
  runs: Run[];
  includeOwner: boolean;
  isExpanded: boolean;
  isRunHistoryExpanded: boolean;
  isPendingRun: boolean;
  runsDataLoading: boolean;
  editingAutomationId: number | null;
  editingName: string;
  onToggleRow: (automationId: number) => void;
  onToggleRunHistory: (automationId: number) => void;
  onRunAutomation: (event: ReactMouseEvent<HTMLButtonElement>, automationId: number, status: string) => void;
  onChatAutomation: (
    event: ReactMouseEvent<HTMLButtonElement>,
    automationId: number,
    automationName: string
  ) => void;
  onDebugAutomation: (event: ReactMouseEvent<HTMLButtonElement>, automationId: number) => void;
  onDeleteAutomation: (event: ReactMouseEvent<HTMLButtonElement>, automationId: number, name: string) => void;
  onStartEditingName: (automationId: number, currentName: string) => void;
  onSaveNameAndExit: (automationId: number) => void;
  onCancelEditing: () => void;
  onEditingNameChange: (name: string) => void;
  onRunActionsClick: (automationId: number, runId: number) => void;
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

function formatRunStatusIcon(status: Run["status"]): ReactElement {
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

function renderOwnerCell(automation: AutomationSummary) {
  if (!automation.owner) {
    return <span>-</span>;
  }

  const label = automation.owner.display_name?.trim() || automation.owner.email;
  if (!label) {
    return <span>-</span>;
  }

  return (
    <div className="owner-wrapper">
      {automation.owner.avatar_url && (
        <img src={automation.owner.avatar_url} alt="" className="owner-avatar" aria-hidden="true" />
      )}
      <span>{label}</span>
    </div>
  );
}

function AutomationTableRowComponent({
  automation,
  runs,
  includeOwner,
  isExpanded,
  isRunHistoryExpanded,
  isPendingRun,
  runsDataLoading,
  editingAutomationId,
  editingName,
  onToggleRow,
  onToggleRunHistory,
  onRunAutomation,
  onChatAutomation,
  onDebugAutomation,
  onDeleteAutomation,
  onStartEditingName,
  onSaveNameAndExit,
  onCancelEditing,
  onEditingNameChange,
  onRunActionsClick,
}: AutomationTableRowProps) {
  const successStats = computeRunSuccessStats(runs);
  const lastRunIndicator = determineLastRunIndicator(runs);
  const isRunning = automation.status === "running";
  const createdDisplay = formatDateTimeShort(automation.created_at ?? null);
  const lastRunDisplay = formatDateTimeShort(automation.last_run_at ?? null);
  const nextRunDisplay = formatDateTimeShort(automation.next_run_at ?? null);
  const emptyColspan = includeOwner ? 8 : 7;

  const handleRowKeyDown = (event: ReactKeyboardEvent<HTMLTableRowElement>) => {
    const key = event.key;
    if (key === "Enter") {
      event.preventDefault();
      onToggleRow(automation.id);
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
    const rows = Array.from(tbody.querySelectorAll<HTMLTableRowElement>("tr[data-automation-id]"));
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
        data-automation-id={automation.id}
        className={clsx('fiche-row', automation.status === "error" && "error-row")}
        onClick={() => onToggleRow(automation.id)}
        onKeyDown={handleRowKeyDown}
      >
        <Table.Cell data-label="Name" className="name-cell">
          {editingAutomationId === automation.id ? (
            <input
              className="inline-edit-input"
              value={editingName}
              onChange={(e) => onEditingNameChange(e.target.value)}
              onBlur={() => onSaveNameAndExit(automation.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.stopPropagation();
                  onSaveNameAndExit(automation.id);
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
              onClick={() => onStartEditingName(automation.id, automation.name)}
              title="Click to rename"
            >
              {automation.name}
            </span>
          )}
        </Table.Cell>
        {includeOwner && (
          <Table.Cell className="owner-cell" data-label="Owner">
            {renderOwnerCell(automation)}
          </Table.Cell>
        )}
        <Table.Cell data-label="Status">
          <Badge variant={automation.status === 'error' ? 'error' : automation.status === 'running' || automation.status === 'processing' ? 'warning' : 'success'}>
            {formatStatus(automation.status)}
          </Badge>
          {automation.last_error && automation.last_error.trim() && (
            <span className="info-icon" title={automation.last_error}>
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
              data-testid={`run-automation-${automation.id}`}
              disabled={isRunning || isPendingRun}
              title={isRunning ? "Automation is already running" : "Run automation"}
              onClick={(event) => onRunAutomation(event, automation.id, automation.status)}
            >
              <PlayIcon />
            </IconButton>
            <IconButton
              className="chat-btn"
              data-testid={`chat-automation-${automation.id}`}
              title="Open automation chat"
              onClick={(event) => onChatAutomation(event, automation.id, automation.name)}
            >
              <MessageCircleIcon />
            </IconButton>
            <IconButton
              className="debug-btn"
              data-testid={`debug-automation-${automation.id}`}
              title="Automation settings"
              onClick={(event) => onDebugAutomation(event, automation.id)}
            >
              <SettingsIcon />
            </IconButton>
            <IconButton
              className="delete-btn"
              data-testid={`delete-automation-${automation.id}`}
              title="Delete automation"
              onClick={(event) => onDeleteAutomation(event, automation.id, automation.name)}
            >
              <TrashIcon />
            </IconButton>
          </div>
        </Table.Cell>
      </Table.Row>
      {isExpanded && (
        <tr className="fiche-detail-row" key={`detail-${automation.id}`}>
          <td colSpan={emptyColspan}>
            <div className="fiche-detail-container">
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
                                  onRunActionsClick(automation.id, run.id);
                                }}
                                onKeyDown={(event) => {
                                  if (event.key === "Enter" || event.key === " ") {
                                    event.preventDefault();
                                    event.stopPropagation();
                                    onRunActionsClick(automation.id, run.id);
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
                        onToggleRunHistory(automation.id);
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
export const AutomationTableRow = memo(AutomationTableRowComponent);
