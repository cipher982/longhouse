import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
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
  elapsedSeconds?: number | null;
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
const CONNECTION_LABELS: Partial<Record<LedgerConnection, string>> = {
  reconnecting: "Updates disconnected",
};
const RECEIPT_ACCENT_MS = 1_300;

function monotonicNow(): number {
  return typeof performance !== "undefined" &&
    typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

function useSystemReducedMotion(): boolean {
  const [matches, setMatches] = useState(false);
  useEffect(() => {
    if (
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    )
      return;
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener?.("change", update);
    return () => media.removeEventListener?.("change", update);
  }, []);
  return matches;
}

function ageText(seconds: number): string {
  const age = Math.max(0, Math.floor(seconds));
  if (age === 0) return "just now";
  if (age < 60) return `${age}s ago`;
  if (age < 3_600) return `${Math.floor(age / 60)}m ${age % 60}s ago`;
  if (age < 86_400)
    return `${Math.floor(age / 3_600)}h ${Math.floor((age % 3_600) / 60)}m ago`;
  return `${Math.floor(age / 86_400)}d ${Math.floor((age % 86_400) / 3_600)}h ago`;
}

function elapsedText(seconds: number): string {
  const elapsed = Math.max(0, Math.floor(seconds));
  return `${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, "0")}`;
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
      ) : tone === "working" ? (
        <path
          d="M9 3.1a5.9 5.9 0 1 0 5.9 5.9"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
      ) : (
        <circle cx="9" cy="9" r="2" fill="currentColor" />
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
  const systemReduceMotion = useSystemReducedMotion();
  const effectiveReduceMotion = reduceMotion || systemReduceMotion;
  const clockedMotion = motionTimeMs !== undefined;
  const workEvidence = state.tone === "working" && state.animateWork;
  const workIsMoving = workEvidence && !effectiveReduceMotion;
  const workAngle = (((motionTimeMs ?? 0) % 7_200) / 7_200) * 360;
  const displayDetail = useMemo(
    () => compactDetail(state.detail, state.detailKind),
    [state.detail, state.detailKind],
  );
  const contentRef = useRef<HTMLDivElement | null>(null);
  const [contentHeight, setContentHeight] = useState<number | undefined>();
  const receiptRef = useRef<HTMLSpanElement | null>(null);
  const receiptAnimation = useRef<Animation | null>(null);
  const receiptTimer = useRef<number | undefined>(undefined);
  const receiptUntil = useRef(0);

  // Only this wrapper owns geometry. New wording is already committed inside
  // it; resizing never fades old claims or changes the composer's dimensions.
  useLayoutEffect(() => {
    const content = contentRef.current;
    if (!content) return;
    const measure = () =>
      setContentHeight(content.getBoundingClientRect().height);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(content);
    return () => observer.disconnect();
  }, []);

  useLayoutEffect(() => {
    const node = receiptRef.current;
    if (!node) return;
    const stop = () => {
      receiptAnimation.current?.cancel();
      receiptAnimation.current = null;
      window.clearTimeout(receiptTimer.current);
      receiptUntil.current = 0;
      node.style.opacity = "0";
    };
    if (!workEvidence) {
      const opacity = getComputedStyle(node).opacity;
      stop();
      if (!effectiveReduceMotion && Number(opacity) > 0) {
        receiptAnimation.current = node.animate([{ opacity }, { opacity: 0 }], {
          duration: 120,
          easing: "cubic-bezier(0.2, 0.8, 0.2, 1)",
        });
      }
      return;
    }
    // Changing the accessibility preference cancels the previous accent too.
    stop();
    return activityFeed?.subscribe((frame) => {
      if (!frame || monotonicNow() < receiptUntil.current) return;
      stop();
      receiptUntil.current = monotonicNow() + RECEIPT_ACCENT_MS;
      if (effectiveReduceMotion) {
        node.style.opacity = "0.28";
        receiptTimer.current = window.setTimeout(stop, RECEIPT_ACCENT_MS);
      } else {
        receiptAnimation.current = node.animate(
          [{ opacity: 0 }, { opacity: 0.9, offset: 0.12 }, { opacity: 0 }],
          {
            duration: RECEIPT_ACCENT_MS,
            easing: "cubic-bezier(0.2, 0.8, 0.2, 1)",
          },
        );
      }
    });
  }, [activityFeed, workEvidence, effectiveReduceMotion]);
  useLayoutEffect(
    () => () => {
      receiptAnimation.current?.cancel();
      window.clearTimeout(receiptTimer.current);
    },
    [],
  );

  const connectionLabel = CONNECTION_LABELS[state.connection] ?? null;
  const showContext =
    state.tone === "unknown" ||
    state.tone === "attention" ||
    connectionLabel !== null;
  const prominence = showContext
    ? "expanded"
    : notice && surface !== "dock"
      ? "notice"
      : "rest";
  const heartbeatDescription =
    state.heartbeatAgeMs === null
      ? "Viewer heartbeat is scoped to updates, not provider work"
      : `Last update heartbeat ${ageText(state.heartbeatAgeMs / 1_000)}`;
  const showElapsed = state.elapsedSeconds != null && state.tone !== "unknown";

  return (
    <div
      className="session-ledger"
      data-testid={testId}
      data-work-state={state.tone}
      data-work-motion={workIsMoving ? "active" : "off"}
      data-clocked-motion={clockedMotion ? "true" : "false"}
      data-connection={state.connection}
      data-reduce-motion={effectiveReduceMotion ? "true" : "false"}
      data-surface={surface}
      data-prominence={prominence}
    >
      <span className="session-ledger__work-sheen" aria-hidden="true">
        <span />
      </span>
      <span
        ref={receiptRef}
        className="session-ledger__receipt-flash"
        aria-hidden="true"
      />
      <div className="session-ledger__history" aria-hidden="true">
        {activityFeed && state.connection !== "recorded" ? (
          <ActivityStrip
            feed={activityFeed}
            tone="live"
            height={76}
            reduceMotion={effectiveReduceMotion}
            showHistory={workEvidence}
            label="Received updates"
            title="Received updates, not proof of provider progress"
          />
        ) : workEvidence && state.connection !== "recorded" ? (
          <span
            className="session-ledger__receipts"
            data-testid="receipt-trail"
          >
            {state.receiptMarks.map((mark) => {
              if (mark.ageMs < 0 || mark.ageMs >= RECEIPT_WINDOW_MS)
                return null;
              const freshness = 1 - mark.ageMs / RECEIPT_WINDOW_MS;
              const position = effectiveReduceMotion
                ? (mark.sequence % 22) / 21
                : 1 - freshness;
              return (
                <span
                  key={mark.id}
                  className="session-ledger__receipt"
                  data-replay={mark.replay ? "true" : "false"}
                  data-receipt-id={mark.id}
                  style={{
                    right: `${position * 100}%`,
                    opacity: effectiveReduceMotion ? 1 : freshness,
                  }}
                />
              );
            })}
          </span>
        ) : null}
      </div>
      <span
        className="sr-only"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {connectionLabel ? state.observation : ""}
      </span>
      <div
        className="session-ledger__status-content"
        style={{ height: contentHeight }}
      >
        <div ref={contentRef} className="session-ledger__status-content-inner">
          <details className="session-ledger__observation">
            <summary
              className="session-ledger__primary"
              title="Inspect the observed session evidence"
              aria-label={`Inspect evidence: ${state.headline}${connectionLabel ? `. ${connectionLabel}` : ""}.`}
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
              <span className="session-ledger__operation">
                <span
                  className="session-ledger__headline"
                  role="status"
                  aria-live="polite"
                  aria-atomic="true"
                >
                  {state.headline}
                </span>
                {displayDetail && state.detailKind === "literal" ? (
                  <span
                    className="session-ledger__detail"
                    title={state.detail ?? undefined}
                  >
                    {state.tone === "unknown" ? "Last observed: " : ""}
                    {displayDetail}
                  </span>
                ) : null}
              </span>
              {showElapsed ? (
                <span
                  className="session-ledger__elapsed"
                  aria-label={`Elapsed ${elapsedText(state.elapsedSeconds!)}`}
                >
                  {elapsedText(state.elapsedSeconds!)}
                </span>
              ) : (
                <span />
              )}
              <span className="session-ledger__info" aria-hidden="true">
                <svg viewBox="0 0 20 20" fill="none">
                  <circle
                    cx="10"
                    cy="10"
                    r="7"
                    stroke="currentColor"
                    strokeWidth="1.3"
                  />
                  <path
                    d="M10 9v5m0-8v.2"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    strokeLinecap="round"
                  />
                </svg>
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
                    <dd className="session-ledger__full-detail">
                      {state.detail}
                    </dd>
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
            </div>
          </details>
          {prominence !== "rest" ? (
            <div className="session-ledger__context">
              {showContext ? (
                <p className="session-ledger__note">
                  {state.detailKind === "explanation" && state.detail
                    ? state.detail
                    : state.observation}
                </p>
              ) : notice && surface !== "dock" ? (
                <p className="session-ledger__note">{notice}</p>
              ) : null}
              {actionSlot}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
