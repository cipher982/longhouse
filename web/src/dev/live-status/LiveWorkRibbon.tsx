import { useMemo, type CSSProperties } from "react";
import type { LiveWorkRibbonProps, RibbonState } from "./types";
import "./LiveWorkRibbon.css";

const RECEIPT_WINDOW_MS = 12_000;
const HEARTBEAT_GLINT_MS = 420;
const WORK_ROTATION_MS = 7_200;

function ageText(seconds: number): string {
  const age = Math.max(0, Math.floor(seconds));
  if (age === 0) return "just now";
  if (age < 60) return `${age}s ago`;
  if (age < 3_600) return `${Math.floor(age / 60)}m ${age % 60}s ago`;
  if (age < 86_400)
    return `${Math.floor(age / 3_600)}h ${Math.floor((age % 3_600) / 60)}m ago`;
  return `${Math.floor(age / 86_400)}d ${Math.floor((age % 86_400) / 3_600)}h ago`;
}

function WorkGlyph({ tone }: { tone: RibbonState["tone"] }) {
  return (
    <svg viewBox="0 0 18 18" fill="none" aria-hidden="true" focusable="false">
      {tone === "working" ? (
        <g stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
          <path d="M9 2.5a6.5 6.5 0 0 1 5.63 3.25" />
          <path d="M14.63 12.25A6.5 6.5 0 0 1 9 15.5" />
          <path d="M3.37 12.25a6.5 6.5 0 0 1 0-6.5" />
        </g>
      ) : tone === "unknown" ? (
        <g
          stroke="currentColor"
          strokeWidth="1.35"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <rect x="3" y="3" width="12" height="12" rx="3" />
          <path d="M7.4 6.6a1.75 1.75 0 0 1 3.35.7c0 1.2-1.75 1.35-1.75 2.45" />
          <path d="M9 12h.01" strokeWidth="1.8" />
        </g>
      ) : tone === "attention" ? (
        <g
          stroke="currentColor"
          strokeWidth="1.4"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="m8.05 2.9-5.8 10.05a1.1 1.1 0 0 0 .95 1.65h11.6a1.1 1.1 0 0 0 .95-1.65L9.95 2.9a1.1 1.1 0 0 0-1.9 0Z" />
          <path d="M9 6.5v3.4M9 12.2h.01" />
        </g>
      ) : (
        <g stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
          <circle cx="9" cy="9" r="6" />
          <path d="M6.5 9h5" />
        </g>
      )}
    </svg>
  );
}

export function LiveWorkRibbon({
  state,
  motionTimeMs,
  reduceMotion,
}: LiveWorkRibbonProps) {
  const workIsMoving =
    state.tone === "working" && state.animateWork && !reduceMotion;
  const workAngle =
    ((motionTimeMs % WORK_ROTATION_MS) / WORK_ROTATION_MS) * 360;
  const heartbeatGlint =
    !reduceMotion &&
    state.connection === "connected" &&
    state.heartbeatAgeMs !== null
      ? Math.max(0, 1 - Math.max(0, state.heartbeatAgeMs) / HEARTBEAT_GLINT_MS)
      : 0;
  const outputAge =
    state.outputAgeSeconds === null
      ? null
      : `Output received ${ageText(state.outputAgeSeconds)}`;
  const heartbeatDescription =
    state.heartbeatAgeMs === null
      ? "No link receipt recorded"
      : `Last link receipt ${ageText(state.heartbeatAgeMs / 1_000)}`;
  const displayDetail = useMemo(() => {
    if (
      !state.detail ||
      state.detailKind !== "literal" ||
      /\s|:\/\//.test(state.detail)
    )
      return state.detail;
    const parts = state.detail.split("/");
    return parts.length > 3 ? `…/${parts.slice(-2).join("/")}` : state.detail;
  }, [state.detail, state.detailKind]);

  return (
    <div
      className="lwr-ribbon"
      data-testid="live-work-ribbon"
      data-work-state={state.tone}
      data-work-motion={workIsMoving ? "active" : "off"}
      data-connection={state.connection}
      data-reduce-motion={reduceMotion ? "true" : "false"}
    >
      <div className="lwr-primary">
        <span
          className="lwr-work-glyph"
          style={
            workIsMoving ? { transform: `rotate(${workAngle}deg)` } : undefined
          }
        >
          <WorkGlyph tone={state.tone} />
        </span>
        <span
          className="lwr-headline"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          {state.headline}
        </span>
        <span
          className="lwr-receipt-trail"
          data-testid="receipt-trail"
          aria-hidden="true"
        >
          {state.receiptMarks.map((mark) => {
            if (mark.ageMs < 0 || mark.ageMs >= RECEIPT_WINDOW_MS) return null;
            const freshness = 1 - mark.ageMs / RECEIPT_WINDOW_MS;
            return (
              <span
                key={mark.id}
                className="lwr-receipt-mark"
                data-replay={mark.replay ? "true" : "false"}
                data-receipt-id={mark.id}
                style={{
                  right: `${reduceMotion ? (mark.sequence % 22) * 4 : (1 - freshness) * 85}px`,
                  opacity: reduceMotion ? 1 : freshness,
                }}
              />
            );
          })}
        </span>
      </div>

      {state.detail && (
        <div
          className={
            state.detailKind === "literal" ? "lwr-command" : "lwr-note"
          }
          title={state.detail}
        >
          {displayDetail}
        </div>
      )}

      <details className="lwr-observation">
        <summary
          className="lwr-observation-toggle"
          title="Inspect the observed session evidence"
        >
          <span
            className="lwr-heartbeat"
            aria-hidden="true"
            style={{ "--lwr-heartbeat-glint": heartbeatGlint } as CSSProperties}
          />
          <span className="lwr-observation-copy">
            <span>{state.observation}</span>
            {outputAge && <span className="lwr-output-age">{outputAge}</span>}
          </span>
          <svg
            className="lwr-disclosure"
            viewBox="0 0 16 16"
            fill="none"
            aria-hidden="true"
            focusable="false"
          >
            <path
              d="m5.5 6.5 2.5 2.5 2.5-2.5"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </summary>
        <div className="lwr-evidence">
          <dl className="lwr-facts">
            {state.detail && (
              <div className="lwr-fact">
                <dt>Detail</dt>
                <dd className="lwr-full-command">{state.detail}</dd>
              </div>
            )}
            <div className="lwr-fact">
              <dt>Link receipt</dt>
              <dd>{heartbeatDescription}</dd>
            </div>
            {state.facts.map((fact, index) => (
              <div className="lwr-fact" key={`${fact.label}-${index}`}>
                <dt>{fact.label}</dt>
                <dd>{fact.value}</dd>
              </div>
            ))}
          </dl>
          <p className="lwr-receipt-key">
            Receipt marks show content received over the last 12 seconds, not
            work progress. Hollow marks are replayed content.
          </p>
        </div>
      </details>
    </div>
  );
}
