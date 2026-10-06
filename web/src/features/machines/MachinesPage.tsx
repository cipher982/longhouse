import { useState, type ReactNode } from "react";
import { Link } from "react-router";
import type { MachineDirectoryEntry, MachineSummary } from "@/shared/api/index";
import { Button, EmptyState, PageShell, Spinner } from "@/shared/ui";
import { PlusIcon } from "@/shared/ui/icons";
import { ProviderGlyph } from "@/shared/ui/ProviderGlyph";
import { useReadinessFlag } from "@/shared/lib/readiness-contract";
import { getProviderLabel } from "@/shared/lib/providers";
import AddRunnerModal from "@/features/runners/AddRunnerModal";
import { useRunners } from "@/features/runners/useRunners";
import { ActivityBars } from "./ActivityBars";
import {
  connectionLine,
  isImporting,
  machineAgents,
  machineStatus,
  relativeTime,
  unmatchedRunners,
} from "./machinePresentation";
import { useMachineDirectoryForSummary, useMachineSummaries } from "./useMachines";
import "./MachinesPage.css";
import { errorDetails } from "@/shared/ui/errorDetails";

const AGENTS_SHOWN = 5;
const LIVE_SHOWN = 3;

function Agents({ summary }: { summary: MachineSummary }) {
  const agents = machineAgents(summary.machine, summary.activity);
  const shown = agents.slice(0, AGENTS_SHOWN);
  return (
    <div className="machine-agents">
      {shown.map(({ provider, unavailable }) => (
        <span
          key={provider}
          className={unavailable ? "machine-agent machine-agent--unavailable" : "machine-agent"}
          title={unavailable ? `${getProviderLabel(provider)} (signed out)` : getProviderLabel(provider)}
        >
          <ProviderGlyph provider={provider} size={17} variant="bare" />
        </span>
      ))}
      {agents.length > shown.length && <span className="machine-agents-more">+{agents.length - shown.length}</span>}
    </div>
  );
}

function Latest({ summary }: { summary: MachineSummary }) {
  const { activity, machine } = summary;
  if (activity.live_count > 0) {
    const projects = activity.top_projects.map((item) => item.project);
    return (
      <div className="machine-latest">
        <span className="machine-latest-title">
          {activity.sessions_started} {activity.sessions_started === 1 ? "session" : "sessions"} in 14 days
        </span>
        {projects.length > 0 && <span className="machine-meta">mostly {projects.join(", ")}</span>}
      </div>
    );
  }
  const latest = activity.latest_session;
  if (!latest) {
    return (
      <div className="machine-latest machine-latest--empty">
        <span className="machine-latest-title">No recent sessions</span>
        <span className="machine-meta">
          {machine.online && machine.launch.providers.length > 0
            ? `ready to run ${getProviderLabel(machine.launch.default_provider ?? machine.launch.providers[0].provider)}`
            : "open this machine's sessions to see older history"}
        </span>
      </div>
    );
  }
  return (
    <div className="machine-latest">
      <span className="machine-latest-title">{latest.title || "Untitled session"}</span>
      <span className="machine-meta">
        {[relativeTime(latest.last_activity_at), latest.project].filter(Boolean).join(" · ")}
      </span>
    </div>
  );
}

function MachineRow({ summary }: { summary: MachineSummary }) {
  const { machine, activity, sync } = summary;
  const status = machineStatus(summary);
  const href = `/machines/${encodeURIComponent(machine.device_id)}`;
  const extraLive = activity.live_count - Math.min(LIVE_SHOWN, activity.live_sessions.length);
  const importing = isImporting(sync);
  return (
    <li className="machine-row" data-testid={`machine-row-${machine.device_id}`} data-tone={status.tone}>
      <Link to={href} className="machine-row-main">
        <div className="machine-name-cell">
          <span className="machine-name">
            <span className={`machine-dot machine-dot--${status.tone}`} aria-hidden="true" />
            {machine.machine_name}
          </span>
          <span className="machine-meta machine-sub">{connectionLine(machine)}</span>
        </div>
        <div className={`machine-status machine-status--${status.tone}`}>{status.label}</div>
        <Agents summary={summary} />
        <ActivityBars daily={activity.daily} height={22} />
        <Latest summary={summary} />
        <span className="machine-chevron" aria-hidden="true">
          ›
        </span>
      </Link>
      {activity.live_sessions.length > 0 && (
        <ul className="machine-live" aria-label={`Live on ${machine.machine_name}`}>
          {activity.live_sessions.slice(0, LIVE_SHOWN).map((session) => (
            <li key={session.session_id}>
              <Link to={`/timeline/${session.session_id}`} className="machine-live-row">
                {session.provider && <ProviderGlyph provider={session.provider} size={14} variant="bare" />}
                <span className="machine-live-title">{session.title || "Untitled session"}</span>
                <span className="machine-meta">
                  {[session.project, relativeTime(session.last_activity_at)].filter(Boolean).join(" · ")}
                </span>
              </Link>
            </li>
          ))}
          {extraLive > 0 && (
            <li>
              <Link to={`/timeline?device_id=${encodeURIComponent(machine.device_id)}`} className="machine-live-more">
                and {extraLive} more
              </Link>
            </li>
          )}
        </ul>
      )}
      {(status.hint || importing) && (
        <p className={`machine-note machine-note--${status.tone}`}>
          {status.hint ?? "Importing history from this machine. Sessions appear as they arrive."}
          {status.hint && (
            <Link to={href} className="machine-note-action">
              Fix
            </Link>
          )}
        </p>
      )}
    </li>
  );
}

function DirectoryRow({ machine, provisional = false }: { machine: MachineDirectoryEntry; provisional?: boolean }) {
  return (
    <li className="machine-row" data-testid={`${provisional ? "machine-directory-row" : "machine-row"}-${machine.device_id}`}>
      <Link to={`/machines/${encodeURIComponent(machine.device_id)}`} className="machine-directory-row">
        <span className="machine-name">
          <span className={`machine-dot machine-dot--${machine.online ? "idle" : "off"}`} aria-hidden="true" />
          {machine.machine_name}
        </span>
        <span className="machine-meta">{connectionLine(machine)}</span>
      </Link>
    </li>
  );
}

export default function MachinesPage() {
  const { data, isLoading, error, isError, refetch, isRefetchError } = useMachineSummaries();
  const directory = useMachineDirectoryForSummary({ hasData: data !== undefined, isError });
  const { data: runners } = useRunners({ refetchInterval: 30_000 });
  const [showConnect, setShowConnect] = useState(false);
  const [showQuiet, setShowQuiet] = useState(false);

  const waitingForDirectory = isError && !data && directory.isLoading;
  const pageReady = !isLoading && !waitingForDirectory;
  const directoryReady = directory.data?.machines !== undefined;
  useReadinessFlag({ ready: pageReady || directoryReady, screenshotReady: pageReady });

  const summaries = data?.machines ?? [];
  const active = summaries.filter((summary) => !machineStatus(summary).quiet);
  const quiet = summaries.filter((summary) => machineStatus(summary).quiet);
  const live = summaries.reduce((sum, summary) => sum + summary.activity.live_count, 0);
  const liveMachines = summaries.filter((summary) => summary.activity.live_count > 0).length;
  const started = summaries.reduce((sum, summary) => sum + summary.activity.sessions_started, 0);
  const online = summaries.filter((summary) => summary.machine.online).length;
  const machines = data ? summaries.map((summary) => summary.machine) : directory.data?.machines;
  const strays = machines ? unmatchedRunners(runners ?? [], machines) : [];
  const directoryIsEmpty = data === undefined && directory.data?.machines?.length === 0;

  const connectButton = (
    <Button variant="primary" data-testid="machines-connect-button" onClick={() => setShowConnect(true)}>
      <PlusIcon />
      Connect a machine
    </Button>
  );
  const firstMachineEmptyState = (
    <EmptyState
      title="Connect your first machine"
      description="Install Longhouse on a machine and the Claude Code, Codex and other agent sessions it runs show up here and on the Timeline."
      action={
        <Button variant="primary" size="lg" data-testid="machines-connect-first-button" onClick={() => setShowConnect(true)}>
          Connect a machine
        </Button>
      }
    />
  );

  let body: ReactNode;
  if (directoryIsEmpty && isError) {
    body = (
      <>
        <p className="machines-stale" role="status">
          Activity and sync are unavailable.{" "}
          <button type="button" className="machines-link-button" onClick={() => { void refetch(); void directory.refetch(); }}>
            Try again
          </button>
        </p>
        {directory.isError && directory.data && (
          <p className="machines-stale" role="status">Connection information is also last known; it could not be refreshed.</p>
        )}
        {firstMachineEmptyState}
      </>
    );
  } else if (directoryIsEmpty) {
    body = firstMachineEmptyState;
  } else if (isLoading || waitingForDirectory) {
    body = machines?.length ? (
      <>
        <p className="machine-meta" role="status">Loading activity and sync…</p>
        <ul className="machine-list" data-testid="machine-directory-list">
          {machines.map((machine) => <DirectoryRow key={machine.device_id} machine={machine} provisional />)}
        </ul>
      </>
    ) : (
      <div className="machines-loading">
        <Spinner size="md" label="Loading machines" />
      </div>
    );
  } else if (isError && !data) {
    body = (
      <>
        {machines && machines.length > 0 ? (
          <p className="machines-stale" role="status">
            Activity and sync are unavailable.{" "}
            <button type="button" className="machines-link-button" onClick={() => { void refetch(); void directory.refetch(); }}>
              Try again
            </button>
          </p>
        ) : (
          <EmptyState
            variant="error"
            title="Machine activity and sync are unavailable right now"
            description="Longhouse couldn't read your machines' activity."
            details={errorDetails(error)}
            action={
              <Button variant="secondary" onClick={() => { void refetch(); void directory.refetch(); }}>
                Try again
              </Button>
            }
          />
        )}
        {directory.isError && directory.data && (
          <p className="machines-stale" role="status">Connection information is also last known; it could not be refreshed.</p>
        )}
        {machines && machines.length > 0 && (
          <ul className="machine-list" data-testid="machine-list">
            {machines.map((machine) => <DirectoryRow key={machine.device_id} machine={machine} />)}
          </ul>
        )}
      </>
    );
  } else if (summaries.length === 0) {
    body = firstMachineEmptyState;
  } else {
    body = (
      <>
        <ul className="machine-list" data-testid="machine-list">
          {active.map((summary) => (
            <MachineRow key={summary.machine.device_id} summary={summary} />
          ))}
        </ul>
        {quiet.length > 0 && (
          <div className="machines-quiet" data-testid="machines-quiet">
            {showQuiet ? (
              <ul className="machine-list machine-list--quiet">
                {quiet.map((summary) => (
                  <MachineRow key={summary.machine.device_id} summary={summary} />
                ))}
              </ul>
            ) : (
              <p className="machines-quiet-line">
                <span className="machine-meta">Not seen recently: </span>
                {quiet.map((summary, index) => (
                  <span key={summary.machine.device_id}>
                    {index > 0 && ", "}
                    <Link to={`/machines/${encodeURIComponent(summary.machine.device_id)}`}>{summary.machine.machine_name}</Link>
                  </span>
                ))}
              </p>
            )}
            <button type="button" className="machines-link-button" onClick={() => setShowQuiet((value) => !value)}>
              {showQuiet ? "Hide" : "Show all"}
            </button>
          </div>
        )}
      </>
    );
  }

  return (
    <PageShell size="wide" className="machines-page">
      <header className="machines-header">
        <div>
          <h1 className="machines-title">Machines</h1>
          {summaries.length > 0 && (
            <p className="machines-summary" data-testid="machines-summary">
              {live > 0 ? (
                <>
                  <span className="machines-pulse" aria-hidden="true" />
                  <span className="machines-summary-live">{live} live</span> on {liveMachines}{" "}
                  {liveMachines === 1 ? "machine" : "machines"}
                </>
              ) : (
                <>
                  {online} of {summaries.length} {summaries.length === 1 ? "machine" : "machines"} online
                </>
              )}{" "}
              · {started} {started === 1 ? "session" : "sessions"} in the last 14 days
            </p>
          )}
        </div>
        {Boolean(machines?.length) && connectButton}
      </header>
      {isRefetchError && data && (
        <p className="machines-stale" role="status">
          Could not refresh. Showing what Longhouse knew {relativeTime(data.generated_at) ?? "earlier"}.{" "}
          <button type="button" className="machines-link-button" onClick={() => void refetch()}>
            Retry
          </button>
        </p>
      )}
      {body}
      {!isLoading && strays.length > 0 && (
        <p className="machines-runners machine-meta" data-testid="machines-unmatched-runners">
          Runners not tied to a machine:{" "}
          {strays.map((runner, index) => (
            <span key={runner.id}>
              {index > 0 && ", "}
              <Link to={`/runners/${runner.id}`}>{runner.name}</Link> ({runner.status})
            </span>
          ))}
        </p>
      )}
      {showConnect && <AddRunnerModal isOpen={showConnect} onClose={() => setShowConnect(false)} />}
    </PageShell>
  );
}
