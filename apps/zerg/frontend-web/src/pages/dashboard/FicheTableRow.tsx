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
import type { FicheSummary, Run } from "../../services/api";
import { Table, Badge, IconButton } from "../../components/ui";
import { formatDateTimeShort, formatDuration, capitaliseFirst, formatTokens, formatCost } from "./formatters";
import { computeRunSuccessStats, determineLastRunIndicator } from "./sorting";

interface FicheTableRowProps {
  fiche: FicheSummary;
  runs: Run[];
  includeOwner: boolean;
  isExpanded: boolean;
  isRunHistoryExpanded: boolean;
  isPendingRun: boolean;
  runsDataLoading: boolean;
  editingFicheId: number | null;
  editingName: string;
  onToggleRow: (ficheId: number) => void;
  onToggleRunHistory: (ficheId: number) => void;
  onRunFiche: (event: ReactMouseEvent<HTMLButtonElement>, ficheId: number, status: string) => void;
  onChatFiche: (event: ReactMouseEvent<HTMLButtonElement>, ficheId: number, ficheName: string) => void;
  onDebugFiche: (event: ReactMouseEvent<HTMLButtonElement>, ficheId: number) => void;
  onDeleteFiche: (event: ReactMouseEvent<HTMLButtonElement>, ficheId: number, name: string) => void;
  onStartEditingName: (ficheId: number, currentName: string) => void;
  onSaveNameAndExit: (ficheId: number) => void;
  onCancelEditing: () => void;
  onEditingNameChange: (name: string) => void;
  onRunActionsClick: (ficheId: number, runId: number) => void;
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

function renderOwnerCell(fiche: FicheSummary) {
  if (!fiche.owner) {
    return <span>-</span>;
  }

  const label = fiche.owner.display_name?.trim() || fiche.owner.email;
  if (!label) {
    return <span>-</span>;
  }

  return (
    <div className="owner-wrapper">
      {fiche.owner.avatar_url && <img src={fiche.owner.avatar_url} alt="" className="owner-avatar" aria-hidden="true" />}
      <span>{label}</span>
    </div>
  );
}

function FicheTableRowComponent({
  fiche,
  runs,
  includeOwner,
  isExpanded,
  isRunHistoryExpanded,
  isPendingRun,
  runsDataLoading,
  editingFicheId,
  editingName,
  onToggleRow,
  onToggleRunHistory,
  onRunFiche,
  onChatFiche,
  onDebugFiche,
  onDeleteFiche,
  onStartEditingName,
  onSaveNameAndExit,
  onCancelEditing,
  onEditingNameChange,
  onRunActionsClick,
}: FicheTableRowProps) {
  const successStats = computeRunSuccessStats(runs);
  const lastRunIndicator = determineLastRunIndicator(runs);
  const isRunning = fiche.status === "running";
  const createdDisplay = formatDateTimeShort(fiche.created_at ?? null);
  const lastRunDisplay = formatDateTimeShort(fiche.last_run_at ?? null);
  const nextRunDisplay = formatDateTimeShort(fiche.next_run_at ?? null);
  const emptyColspan = includeOwner ? 8 : 7;

  const handleRowKeyDown = (event: ReactKeyboardEvent<HTMLTableRowElement>) => {
    const key = event.key;
    if (key === "Enter") {
      event.preventDefault();
      onToggleRow(fiche.id);
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
    const rows = Array.from(tbody.querySelectorAll<HTMLTableRowElement>("tr[data-fiche-id]"));
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
        data-fiche-id={fiche.id}
        aria-expanded={isExpanded}
        className={clsx('fiche-row', fiche.status === "error" && "error-row")}
        onClick={() => onToggleRow(fiche.id)}
        onKeyDown={handleRowKeyDown}
      >
        <Table.Cell data-label="Name" className="name-cell">
          {editingFicheId === fiche.id ? (
            <input
              className="inline-edit-input"
              value={editingName}
              onChange={(e) => onEditingNameChange(e.target.value)}
              onBlur={() => onSaveNameAndExit(fiche.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.stopPropagation();
                  onSaveNameAndExit(fiche.id);
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
              onClick={() => onStartEditingName(fiche.id, fiche.name)}
              title="Click to rename"
            >
              {fiche.name}
            </span>
          )}
        </Table.Cell>
        {includeOwner && (
          <Table.Cell className="owner-cell" data-label="Owner">
            {renderOwnerCell(fiche)}
          </Table.Cell>
        )}
        <Table.Cell data-label="Status">
          <Badge variant={fiche.status === 'error' ? 'error' : fiche.status === 'running' || fiche.status === 'processing' ? 'warning' : 'success'}>
            {formatStatus(fiche.status)}
          </Badge>
          {fiche.last_error && fiche.last_error.trim() && (
            <span className="info-icon" title={fiche.last_error}>
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
              data-testid={`run-fiche-${fiche.id}`}
              disabled={isRunning || isPendingRun}
              title={isRunning ? "Fiche is already running" : "Run Fiche"}
              onClick={(event) => onRunFiche(event, fiche.id, fiche.status)}
            >
              <PlayIcon />
            </IconButton>
            <IconButton
              className="chat-btn"
              data-testid={`chat-fiche-${fiche.id}`}
              title="Chat with Fiche"
              onClick={(event) => onChatFiche(event, fiche.id, fiche.name)}
            >
              <MessageCircleIcon />
            </IconButton>
            <IconButton
              className="debug-btn"
              data-testid={`debug-fiche-${fiche.id}`}
              title="Debug / Info"
              onClick={(event) => onDebugFiche(event, fiche.id)}
            >
              <SettingsIcon />
            </IconButton>
            <IconButton
              className="delete-btn"
              data-testid={`delete-fiche-${fiche.id}`}
              title="Delete Fiche"
              onClick={(event) => onDeleteFiche(event, fiche.id, fiche.name)}
            >
              <TrashIcon />
            </IconButton>
          </div>
        </Table.Cell>
      </Table.Row>
      {isExpanded && (
        <tr className="fiche-detail-row" key={`detail-${fiche.id}`}>
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
                                  onRunActionsClick(fiche.id, run.id);
                                }}
                                onKeyDown={(event) => {
                                  if (event.key === "Enter" || event.key === " ") {
                                    event.preventDefault();
                                    event.stopPropagation();
                                    onRunActionsClick(fiche.id, run.id);
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
                        onToggleRunHistory(fiche.id);
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
export const FicheTableRow = memo(FicheTableRowComponent);
