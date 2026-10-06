import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import type { MachineActivity, MachineDirectoryEntry, MachineSummary, Runner } from "@/shared/api/index";
import { Button, EmptyState, PageShell, Spinner } from "@/shared/ui";
import { ProviderGlyph } from "@/shared/ui/ProviderGlyph";
import { useReadinessFlag } from "@/shared/lib/readiness-contract";
import { getProviderLabel } from "@/shared/lib/providers";
import LaunchSessionModal from "@/features/launch/LaunchSessionModal";
import ProviderSignInList from "@/features/launch/ProviderSignInList";
import { useRunners } from "@/features/runners/useRunners";
import { ActivityBars } from "./ActivityBars";
import {
  activityColor,
  connectionLine,
  durationSince,
  historyLine,
  machineStatus,
  providerTotals,
  relativeTime,
  runnerForMachine,
} from "./machinePresentation";
import { useMachineDirectoryForSummary, useMachineSummaries } from "./useMachines";
import "./MachinesPage.css";

function shortDate(isoDate: string): string {
  // A calendar day from the server, not an instant: render it without a timezone shift.
  const [year, month, day] = isoDate.split("-").map(Number);
  return new Date(year, month - 1, day).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function Agents({ machine, activity }: { machine: MachineDirectoryEntry; activity?: MachineActivity }) {
  // Only what the machine can start now reads Ready, most used first. History
  // never stands in for readiness.
  const usage = Object.fromEntries(providerTotals(activity));
  const ready = machine.launch.providers
    .map((option) => option.provider)
    .sort((a, b) => (usage[b] ?? 0) - (usage[a] ?? 0) || a.localeCompare(b));
  const readiness = machine.provider_readiness ?? {};
  // Everything the engine reported, so an installed agent that has not been
  // checked reads as installed rather than vanishing.
  const reported = Object.keys(readiness).filter(
    (provider) => !ready.includes(provider) && readiness[provider]?.state !== "cli_missing" && readiness[provider]?.state !== "not_authenticated",
  );
  if (!machine.online) {
    const used = providerTotals(activity).map(([provider]) => provider);
    return (
      <>
        <p className="machine-empty-line">
          {used.length > 0
            ? `Ran ${used.map(getProviderLabel).join(", ")} in the last 14 days. Agent readiness is reported while the machine is connected.`
            : "Agent readiness is reported while the machine is connected."}
        </p>
      </>
    );
  }
  return (
    <>
      <ul className="machine-rows">
        {ready.map((provider) => (
          <li key={provider} className="machine-agent-row">
            <ProviderGlyph provider={provider} size={16} variant="bare" />
            <span>{getProviderLabel(provider)}</span>
            <span className="machine-kv-value--live machine-status machine-status--live">Ready</span>
          </li>
        ))}
        {reported.map((provider) => (
          <li key={provider} className="machine-agent-row">
            <ProviderGlyph provider={provider} size={16} variant="bare" />
            <span>{getProviderLabel(provider)}</span>
            <span className="machine-meta">Can't start sessions here</span>
          </li>
        ))}
      </ul>
      <ProviderSignInList machine={machine} />
    </>
  );
}

function Sync({ summary }: { summary: MachineSummary }) {
  const { sync, machine } = summary;
  const history = historyLine(sync);
  if (!sync) {
    return (
      <>
        <div className="machine-kv">
          <span>Remote control</span>
          <span className={machine.online ? "machine-kv-value--live" : "machine-kv-value--muted"}>
            {machine.online ? "Connected" : "Disconnected"}
          </span>
        </div>
        <p className="machine-empty-line">No upload reports from this machine in the last 30 days.</p>
      </>
    );
  }
  return (
    <>
      <div className="machine-kv">
        <span>History</span>
        <span className={`machine-kv-value--${history.tone === "plain" ? "muted" : history.tone}`}>{history.text}</span>
      </div>
      <div className="machine-kv">
        <span>Newest upload</span>
        <span>{relativeTime(sync.last_upload_at) ?? "None yet"}</span>
      </div>
      {sync.upload_p95_ms != null && (
        <div className="machine-kv">
          <span>Upload time, p95</span>
          <span>{sync.upload_p95_ms < 1000 ? `${sync.upload_p95_ms} ms` : `${(sync.upload_p95_ms / 1000).toFixed(1)} s`}</span>
        </div>
      )}
      <div className="machine-kv">
        <span>Waiting to upload</span>
        <span>{sync.waiting_uploads ? sync.waiting_uploads.toLocaleString() : "Nothing"}</span>
      </div>
      {Boolean(sync.failed_uploads) && (
        <div className="machine-kv">
          <span>Failed uploads</span>
          <span className="machine-kv-value--attention">{sync.failed_uploads?.toLocaleString()}</span>
        </div>
      )}
      <div className="machine-kv">
        <span>Remote control</span>
        <span className={machine.online ? "machine-kv-value--live" : "machine-kv-value--muted"}>
          {machine.online ? "Connected" : "Disconnected"}
        </span>
      </div>
      {sync.stale && (
        <p className="machine-empty-line">Last report {relativeTime(sync.reported_at) ?? "a while ago"}; these numbers may be out of date.</p>
      )}
      {!sync.stale && sync.status !== "healthy" && sync.status !== "unknown" && (
        <p className="machine-empty-line">{sync.status_summary}</p>
      )}
    </>
  );
}

function RunnerTools({ runner }: { runner: Runner }) {
  return (
    <section className="machine-section" data-testid="machine-runner-tools">
      <h2 className="machine-section-title">
        <span>
          Runner tools <span className="machine-meta">· optional</span>
        </span>
      </h2>
      <div className="machine-kv">
        <span>Runner “{runner.name}” lets automations run shell commands on this machine.</span>
        <span>
          <span className={runner.status === "online" ? "machine-kv-value--live" : "machine-kv-value--muted"}>
            {runner.status === "online" ? "Online" : "Offline"}
          </span>
          {runner.runner_version && <span className="machine-meta"> · v{runner.runner_version}</span>}
          {" · "}
          <Link to={`/runners/${runner.id}`} className="machine-meta">
            Manage
          </Link>
        </span>
      </div>
    </section>
  );
}

export default function MachineDetailPage() {
  const { deviceId = "" } = useParams();
  const navigate = useNavigate();
  const { data, isLoading, isError, error, refetch } = useMachineSummaries();
  const directory = useMachineDirectoryForSummary({ hasData: data !== undefined, isError });
  const { data: runners } = useRunners({ refetchInterval: 30_000 });
  const directoryMachine = directory.data?.machines?.find((machine) => machine.device_id === deviceId);
  const waitingForDirectory = isError && !data && directory.isLoading;
  const [launchOpen, setLaunchOpen] = useState(false);

  const pageReady = !isLoading && !waitingForDirectory;
  useReadinessFlag({ ready: pageReady || Boolean(directoryMachine), screenshotReady: pageReady });

  if ((isLoading || waitingForDirectory) && !directoryMachine) {
    return (
      <PageShell size="wide" className="machine-page">
        <div className="machines-loading">
          <Spinner size="md" label="Loading machine" />
        </div>
      </PageShell>
    );
  }
  if (isError && !data && !directoryMachine) {
    return (
      <PageShell size="wide" className="machine-page">
        <EmptyState
          variant="error"
          title="Machine information is unavailable right now"
          description={error instanceof Error ? error.message : "Longhouse could not read this host's machines."}
          action={
            <Button variant="secondary" onClick={() => { void refetch(); void directory.refetch(); }}>
              Try again
            </Button>
          }
        />
      </PageShell>
    );
  }
  const summary = data?.machines.find((item) => item.machine.device_id === deviceId);
  const machine = summary?.machine ?? (!data ? directoryMachine : undefined);
  if (!machine) {
    return (
      <PageShell size="wide" className="machine-page">
        <EmptyState
          title="Machine not found"
          description="It may have been removed from this Longhouse."
          action={
            <Link to="/machines" className="ui-button ui-button--secondary ui-button--md">
              All machines
            </Link>
          }
        />
      </PageShell>
    );
  }

  const activity = summary?.activity;
  const sync = summary?.sync;
  const status = summary
    ? machineStatus(summary)
    : { tone: machine.online ? "idle" : "off", label: machine.online ? "Online" : "Offline", hint: null };
  const runner = runnerForMachine(runners ?? [], machine);
  const timelineHref = `/timeline?device_id=${encodeURIComponent(machine.device_id)}`;
  const totals = providerTotals(activity);
  const canLaunch = machine.launch.providers.length > 0;

  return (
    <PageShell size="wide" className="machine-page">
      <nav className="machine-crumb" aria-label="Breadcrumb">
        <Link to="/machines">Machines</Link> / {machine.machine_name}
      </nav>
      <header className="machine-head">
        <div>
          <h1 className="machine-head-name" data-testid="machine-name">
            <span className={`machine-dot machine-dot--${status.tone}`} aria-hidden="true" />
            {machine.machine_name}
          </h1>
          <p className="machine-head-line">
            <span className={`machine-status machine-status--${status.tone}`}>{status.label}</span> · {connectionLine(machine)}
          </p>
        </div>
        <div className="machine-head-actions">
          <Link to={timelineHref} className="ui-button ui-button--ghost ui-button--md machine-link-button">
            Open sessions
          </Link>
          {canLaunch && (
            <Button variant="primary" data-testid="machine-new-session" onClick={() => setLaunchOpen(true)}>
              New session
            </Button>
          )}
        </div>
      </header>
      {isLoading && !summary && (
        <p className="machine-meta" role="status">Loading activity and sync…</p>
      )}

      {isError && (
        <p className="machines-stale" role="status">
          {data
            ? `Could not refresh. Showing what Longhouse knew ${relativeTime(data.generated_at) ?? "earlier"}.`
            : "Activity and sync are unavailable. Connection and launch information comes from the machine directory."}{" "}
          {directory.isError && !data && "Directory information could not be refreshed and is also last known. "}
          <button type="button" className="machines-link-button" onClick={() => { void refetch(); void directory.refetch(); }}>
            Retry
          </button>
        </p>
      )}
      {status.hint && <p className={`machine-note machine-note--${status.tone}`}>{status.hint}</p>}

      {activity && (
      <section className="machine-section">
        <h2 className="machine-section-title">Last 14 days</h2>
        <p className="machine-activity-line">
          {activity.sessions_started} {activity.sessions_started === 1 ? "session" : "sessions"} started
        </p>
        <ActivityBars daily={activity.daily} height={84} size="full" />
        <div className="machine-axis">
          <span>{shortDate(data!.first_day)}</span>
          <span>Today</span>
        </div>
        {totals.length > 0 && (
          <div className="machine-legend">
            {totals.map(([provider, count]) => (
              <span key={provider}>
                <i style={{ background: activityColor(provider) }} />
                {getProviderLabel(provider)} <span className="machine-meta">{count}</span>
              </span>
            ))}
          </div>
        )}
      </section>
      )}

      {activity && activity.live_sessions.length > 0 && (
        <section className="machine-section">
          <h2 className="machine-section-title">
            <span>Live now</span>
            {activity.live_count > activity.live_sessions.length && (
              <Link to={timelineHref} className="machine-meta">
                All {activity.live_count} →
              </Link>
            )}
          </h2>
          <ul className="machine-rows">
            {activity.live_sessions.map((session) => (
              <li key={session.session_id}>
                <Link to={`/timeline/${session.session_id}`} className="machine-session">
                  {session.provider ? <ProviderGlyph provider={session.provider} size={16} variant="bare" /> : <span />}
                  <span>{session.title || "Untitled session"}</span>
                  <span className="machine-meta">
                    {[session.project, relativeTime(session.last_activity_at)].filter(Boolean).join(" · ")}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}

      {activity && activity.live_sessions.length === 0 && activity.latest_session && (
        <section className="machine-section">
          <h2 className="machine-section-title">Latest session</h2>
          <ul className="machine-rows">
            <li>
              <Link to={`/timeline/${activity.latest_session.session_id}`} className="machine-session">
                {activity.latest_session.provider ? (
                  <ProviderGlyph provider={activity.latest_session.provider} size={16} variant="bare" />
                ) : (
                  <span />
                )}
                <span>{activity.latest_session.title || "Untitled session"}</span>
                <span className="machine-meta">
                  {[activity.latest_session.project, relativeTime(activity.latest_session.last_activity_at)].filter(Boolean).join(" · ")}
                </span>
              </Link>
            </li>
          </ul>
        </section>
      )}

      <div className="machine-section machine-columns">
        <section>
          <h2 className="machine-section-title">Agents</h2>
          <Agents machine={machine} activity={activity} />
        </section>
        <section>
          <h2 className="machine-section-title">Sync</h2>
          {summary ? (
            <Sync summary={summary} />
          ) : isLoading ? (
            <p className="machine-empty-line">Loading sync…</p>
          ) : (
            <p className="machine-empty-line">Sync information is unavailable.</p>
          )}
        </section>
      </div>

      {runner && <RunnerTools runner={runner} />}

      <details className="machine-details">
        <summary>Technical details</summary>
        <dl>
          <dt>Device id</dt>
          <dd>{machine.device_id}</dd>
          {machine.engine_build && (
            <>
              <dt>Machine Agent build</dt>
              <dd>{machine.engine_build}</dd>
            </>
          )}
          {sync?.engine_version && (
            <>
              <dt>Machine Agent version</dt>
              <dd>{sync.engine_version}</dd>
            </>
          )}
          {machine.connected_since && (
            <>
              <dt>Connected for</dt>
              <dd>{durationSince(machine.connected_since)}</dd>
            </>
          )}
          {sync && (
            <>
              <dt>Last upload report</dt>
              <dd>
                {relativeTime(sync.reported_at)} ({sync.status}: {sync.status_summary})
              </dd>
            </>
          )}
        </dl>
      </details>

      {launchOpen && (
        <LaunchSessionModal
          isOpen={launchOpen}
          initialDeviceId={machine.device_id}
          onClose={() => setLaunchOpen(false)}
          onLaunched={(sessionId) => {
            setLaunchOpen(false);
            navigate(`/timeline/${sessionId}`);
          }}
        />
      )}
    </PageShell>
  );
}
