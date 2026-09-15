/**
 * Phase 4 (Instruments), web-restyle-signal.md. One isolated component +
 * the ".instrument-readout-rail"/".instrument-readout*" CSS block in
 * styles/instruments.css — both deletable together in one commit.
 *
 * A glass column of Nixie readouts beside the transcript on wide viewports
 * (>= 1180px; hidden below that in CSS). Ships only entries the caller can
 * actually back with real data — never a fabricated "Shipped" row.
 */
import { useQuery } from "@tanstack/react-query";
import { fetchRunnerStatus } from "../../services/api";
import { formatElapsedClock } from "../session-workspace/sessionHeaderState";
import { Nixie } from "./Nixie";

export interface ReadoutRailProps {
  /** Elapsed seconds for the running turn, or the most recently finished
   * turn's duration when idle. `null` omits the "Turn" entry — no elapsed
   * time is known. */
  turnSeconds: number | null;
  turnLive: boolean;
  /** Both required — the entry is omitted entirely if either is missing. */
  contextTokens: number | null;
  contextWindow: number | null;
  toolCallsThisTurn: number | null;
  toolCallsLive: boolean;
  /** Label of the currently running tool row; omitted when nothing is
   * running. */
  waitingOnLabel: string | null;
}

function formatCompactTokens(n: number): string {
  if (n >= 1000) return `${Math.round(n / 1000)}k`;
  return String(Math.round(n));
}

export function ReadoutRail({
  turnSeconds,
  turnLive,
  contextTokens,
  contextWindow,
  toolCallsThisTurn,
  toolCallsLive,
  waitingOnLabel,
}: ReadoutRailProps) {
  // Same query key as the nav's status read (components/Layout.tsx) — the
  // cache is shared, so this doesn't add a second network request.
  const { data: runnerStatus } = useQuery({
    queryKey: ["runnerStatus"],
    queryFn: fetchRunnerStatus,
    staleTime: 15_000,
    retry: false,
  });

  const hasContext = contextTokens != null && contextWindow != null && contextWindow > 0;
  const hasMachines = Boolean(runnerStatus && runnerStatus.total > 0);

  return (
    <div className="instrument-readout-rail" data-testid="session-readout-rail">
      {turnSeconds != null ? (
        <div className="instrument-readout" data-testid="readout-turn">
          <span className="instrument-readout__key">Turn</span>
          <Nixie value={formatElapsedClock(turnSeconds)} dim={!turnLive} />
        </div>
      ) : null}

      {hasContext ? (
        <div className="instrument-readout" data-testid="readout-context">
          <span className="instrument-readout__key">Context</span>
          <Nixie
            value={`${formatCompactTokens(contextTokens as number)} / ${formatCompactTokens(contextWindow as number)}`}
            dim
          />
          <div className="instrument-readout__bar">
            <span
              className="instrument-readout__bar-fill"
              style={{
                width: `${Math.max(
                  0,
                  Math.min(100, Math.round(((contextTokens as number) / (contextWindow as number)) * 100)),
                )}%`,
              }}
            />
          </div>
        </div>
      ) : null}

      {toolCallsThisTurn != null ? (
        <div className="instrument-readout" data-testid="readout-tool-calls">
          <span className="instrument-readout__key">Tool calls this turn</span>
          <Nixie value={toolCallsThisTurn} dim={!toolCallsLive} />
        </div>
      ) : null}

      {waitingOnLabel ? (
        <div className="instrument-readout" data-testid="readout-waiting-on">
          <span className="instrument-readout__key">Waiting on</span>
          <Nixie value={waitingOnLabel} />
        </div>
      ) : null}

      {hasMachines ? (
        <div className="instrument-readout" data-testid="readout-machines">
          <span className="instrument-readout__key">Machines up</span>
          <Nixie value={`${runnerStatus!.online} / ${runnerStatus!.total}`} dim />
        </div>
      ) : null}
    </div>
  );
}
