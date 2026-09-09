import {
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import type { SessionActivityFeed } from "../../lib/sessionActivityFeed";
import { ActivityStrip } from "./ActivityStrip";

export type LedgerTone = "working" | "quiet" | "unknown" | "attention";
export type LedgerConnection =
  | "connected"
  | "reconnecting"
  | "checking"
  | "recorded";
export type LedgerSurface = "dock" | "ledger" | "island";

export interface LedgerReceiptMark {
  id: string;
  ageMs: number;
  sequence: number;
  replay: boolean;
}

export interface SessionLedgerState {
  tone: LedgerTone;
  headline: string;
  detail: string | null;
  detailKind: "literal" | "explanation";
  observation: string;
  connection: LedgerConnection;
  animateWork: boolean;
  outputAgeSeconds: number | null;
  heartbeatAgeMs: number | null;
  receiptMarks: LedgerReceiptMark[];
  facts: Array<{ label: string; value: string }>;
}

export interface SessionLedgerProps {
  state: SessionLedgerState;
  motionTimeMs?: number;
  reduceMotion?: boolean;
  surface?: LedgerSurface;
  notice?: string | null;
  /** Production actions (for example PauseRequestCard) stay outside the renderer. */
  actionSlot?: ReactNode;
  /** Ref-backed stream feed; drawing it does not re-render the workspace per packet. */
  activityFeed?: SessionActivityFeed | null;
  testId?: string;
}

const RECEIPT_WINDOW_MS = 12_000;
const HEARTBEAT_GLINT_MS = 420;
const CONNECTION_LABELS: Record<LedgerConnection, string> = {
  connected: "Updates connected",
  reconnecting: "Updates reconnecting",
  checking: "Checking updates",
  recorded: "Recorded snapshot",
};

function ageText(seconds: number): string {
  const age = Math.max(0, Math.floor(seconds));
  if (age === 0) return "just now";
  if (age < 60) return `${age}s ago`;
  if (age < 3_600) return `${Math.floor(age / 60)}m ${age % 60}s ago`;
  if (age < 86_400)
    return `${Math.floor(age / 3_600)}h ${Math.floor((age % 3_600) / 60)}m ago`;
  return `${Math.floor(age / 86_400)}d ${Math.floor((age % 86_400) / 3_600)}h ago`;
}

function compactDetail(
  detail: string | null,
  kind: SessionLedgerState["detailKind"],
): string | null {
  if (!detail || kind !== "literal" || /\s|:\/\//.test(detail)) return detail;
  const parts = detail.split("/");
  return parts.length > 3 ? `…/${parts.slice(-2).join("/")}` : detail;
}

function WorkGlyph({ tone }: { tone: SessionLedgerState["tone"] }) {
  return (
    <svg viewBox="0 0 18 18" fill="none" aria-hidden="true" focusable="false">
      <circle
        cx="9"
        cy="9"
        r="7"
        stroke="currentColor"
        strokeWidth="1.4"
        opacity=".42"
      />
      {tone === "attention" ? (
        <path
          d="M9 5.2v4.1m0 2.8v.2"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinecap="round"
        />
      ) : tone === "unknown" ? (
        <path
          d="M6.8 6.8a2.35 2.35 0 1 1 3.55 2.03c-.8.44-1.35.8-1.35 1.77m0 2.1v.2"
          stroke="currentColor"
          strokeWidth="1.35"
          strokeLinecap="round"
        />
      ) : (
        <path
          d="M5.4 9h7.2M9 5.4v7.2"
          stroke="currentColor"
          strokeWidth="1.4"
          strokeLinecap="round"
        />
      )}
    </svg>
  );
}

export function SessionLedger({
  state,
  motionTimeMs,
  reduceMotion = false,
  surface = "ledger",
  notice = null,
  actionSlot = null,
  activityFeed = null,
  testId = "session-ledger",
}: SessionLedgerProps) {
  const clockedMotion = motionTimeMs !== undefined;
  const workIsMoving =
    state.tone === "working" && state.animateWork && !reduceMotion;
  const workAngle = (((motionTimeMs ?? 0) % 7_200) / 7_200) * 360;
  const heartbeatGlint =
    !reduceMotion &&
    state.connection === "connected" &&
    state.heartbeatAgeMs !== null
      ? Math.max(0, 1 - Math.max(0, state.heartbeatAgeMs) / HEARTBEAT_GLINT_MS)
      : 0;
  const displayDetail = useMemo(
    () => compactDetail(state.detail, state.detailKind),
    [state.detail, state.detailKind],
  );
  const contextRef = useRef<HTMLDivElement | null>(null);
  const [contextHeight, setContextHeight] = useState(0);
  useLayoutEffect(() => {
    const content = contextRef.current;
    if (!content) return;
    const measure = () =>
      setContextHeight(content.getBoundingClientRect().height);
    measure();
    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(measure);
    observer?.observe(content);
    return () => observer?.disconnect();
  }, []);
  const connectionLabel = CONNECTION_LABELS[state.connection];
  const showContext = state.tone === "unknown" || state.tone === "attention";
  const prominence = showContext
    ? "expanded"
    : notice && surface !== "dock"
      ? "notice"
      : "rest";
  const heartbeatDescription =
    state.heartbeatAgeMs === null
      ? "Viewer heartbeat is scoped to updates, not provider work"
      : `Last update heartbeat ${ageText(state.heartbeatAgeMs / 1_000)}`;

  return (
    <div
      className="session-ledger"
      data-testid={testId}
      data-work-state={state.tone}
      data-work-motion={workIsMoving ? "active" : "off"}
      data-clocked-motion={clockedMotion ? "true" : "false"}
      data-connection={state.connection}
      data-reduce-motion={reduceMotion ? "true" : "false"}
      data-surface={surface}
      data-prominence={prominence}
    >
      <details className="session-ledger__observation">
        <summary
          className="session-ledger__primary"
          title="Inspect the observed session evidence"
          aria-label={`Inspect evidence: ${state.headline}. ${connectionLabel}.`}
        >
          <span
            className="session-ledger__glyph"
            style={
              workIsMoving
                ? { transform: `rotate(${workAngle}deg)` }
                : undefined
            }
          >
            <WorkGlyph tone={state.tone} />
          </span>
          <span
            className="session-ledger__headline"
            role="status"
            aria-live="polite"
            aria-atomic="true"
            title={state.headline}
          >
            {state.headline}
          </span>
          {activityFeed ? (
            <ActivityStrip
              feed={activityFeed}
              tone={
                state.tone === "attention"
                  ? "attention"
                  : state.tone === "working"
                    ? "live"
                    : "idle"
              }
              label="Received updates"
              title="Received updates, not proof of provider progress"
            />
          ) : (
            <span
              className="session-ledger__receipts"
              data-testid="receipt-trail"
              aria-hidden="true"
            >
              {state.receiptMarks.map((mark) => {
                if (mark.ageMs < 0 || mark.ageMs >= RECEIPT_WINDOW_MS)
                  return null;
                const freshness = 1 - mark.ageMs / RECEIPT_WINDOW_MS;
                const position = reduceMotion
                  ? (mark.sequence % 22) / 21
                  : 1 - freshness;
                return (
                  <span
                    key={mark.id}
                    className="session-ledger__receipt"
                    data-replay={mark.replay ? "true" : "false"}
                    data-receipt-id={mark.id}
                    style={{
                      right: `calc(${position * 100}% - ${position * 3}px)`,
                      opacity: reduceMotion ? 1 : freshness,
                    }}
                  />
                );
              })}
            </span>
          )}
          <span className="session-ledger__link">
            <span
              className="session-ledger__heartbeat"
              aria-hidden="true"
              style={
                { "--ledger-heartbeat-glint": heartbeatGlint } as CSSProperties
              }
            />
            <span>{connectionLabel}</span>
            <span className="session-ledger__disclosure" aria-hidden="true">
              ⌄
            </span>
          </span>
        </summary>
        <div className="session-ledger__evidence">
          <dl className="session-ledger__facts">
            <div>
              <dt>Updates</dt>
              <dd>{state.observation}</dd>
            </div>
            {state.outputAgeSeconds !== null ? (
              <div>
                <dt>Output received</dt>
                <dd>{ageText(state.outputAgeSeconds)}</dd>
              </div>
            ) : null}
            {state.detail ? (
              <div>
                <dt>Detail</dt>
                <dd className="session-ledger__full-detail">{state.detail}</dd>
              </div>
            ) : null}
            <div>
              <dt>Heartbeat</dt>
              <dd>{heartbeatDescription}</dd>
            </div>
            {state.facts.map((fact, index) => (
              <div key={`${fact.label}-${index}`}>
                <dt>{fact.label}</dt>
                <dd>{fact.value}</dd>
              </div>
            ))}
          </dl>
          <p className="session-ledger__key">
            Receipt marks show updates received over the last 12 seconds, not
            work progress.
          </p>
        </div>
      </details>
      <div
        className="session-ledger__context"
        style={{ height: prominence === "rest" ? 0 : contextHeight }}
        aria-hidden={
          surface !== "dock" && prominence === "rest" ? true : undefined
        }
      >
        <div className="session-ledger__context-inner" ref={contextRef}>
          {showContext && state.observation !== connectionLabel ? (
            <p className="session-ledger__note">{state.observation}</p>
          ) : null}
          {!showContext && notice && surface !== "dock" ? (
            <p className="session-ledger__note">{notice}</p>
          ) : state.detail &&
            (showContext || state.detailKind === "explanation") ? (
            <p
              className={
                state.detailKind === "literal"
                  ? "session-ledger__command"
                  : "session-ledger__note"
              }
              title={state.detail}
            >
              {displayDetail}
            </p>
          ) : surface !== "dock" ? (
            <p className="session-ledger__note">{state.observation}</p>
          ) : null}
          {actionSlot}
        </div>
      </div>
    </div>
  );
}
