/**
 * Session rail (session-view-terminal-parity C8, flavor A): recent sessions
 * down the left of the session view, each one keystroke away, with the open
 * session expanded into its turns. It lives outside the per-session route,
 * so switching sessions swaps the transcript without remounting the rail.
 *
 * Rows come from the timeline's default first page (the same query and cache
 * entry the unfiltered timeline uses), so the rail adds no endpoint, and no
 * extra fetch when the user arrives from an unfiltered timeline.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Link, useNavigate } from "react-router";
import type { TimelineSessionCard } from "@/shared/api/agents";
import { useAgentSessions } from "@/shared/api/useAgentSessions";
import { useMobileNavSlot } from "@/app/headerSlot";
import { useMediaQuery } from "@/shared/hooks/useMediaQuery";
import { useWallClock } from "@/shared/hooks/useWallClock";
import type { StatusLampState } from "@/shared/instruments/StatusLamp";
import { ProviderGlyph } from "@/shared/ui/ProviderGlyph";
import { SearchIcon, XIcon } from "@/shared/ui/icons";
import { getRowStatus } from "@/features/timeline/SessionRow";
import { buildInboxLayout, historySortKey, isAutomationSession } from "@/features/timeline/timelineInboxModel";
import { getProjectLabel, getSessionCardText } from "@/shared/session/sessionLabels";
import {
  SessionRailContext,
  type RailActiveSession,
  type RailRow,
  type SessionRailContextValue,
} from "./sessionRailContext";
import { useRailPrefetch } from "./useRailPrefetch";
import { SessionSwitcher } from "./SessionSwitcher";
import "./session-rail.css";

/** The timeline's default first page; sharing its filters shares its cache. */
const RAIL_SESSION_FILTERS = { limit: 50 } as const;
const RAIL_HOTKEY_COUNT = 9;

export function isMacPlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  return /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
}

/**
 * The rail's jump keys. Browsers keep ⌘1–⌘9 (Ctrl+1–9 on Windows and Linux)
 * for their own tabs and pages cannot take them, so the rail uses ⌃1–⌃9 on a
 * Mac and Alt+1–9 elsewhere. Returns the zero-based row, or null.
 */
export function railHotkeyIndex(
  event: Pick<KeyboardEvent, "code" | "ctrlKey" | "altKey" | "metaKey" | "shiftKey">,
  mac: boolean,
): number | null {
  const match = /^Digit([1-9])$/.exec(event.code);
  if (!match || event.shiftKey || event.metaKey) return null;
  const modifierHeld = mac ? event.ctrlKey && !event.altKey : event.altKey && !event.ctrlKey;
  return modifierHeld ? Number(match[1]) - 1 : null;
}

export function railHotkeyLabel(index: number, mac: boolean): string {
  return mac ? `⌃${index + 1}` : `Alt+${index + 1}`;
}

/** ⌘K on a Mac, Ctrl+K elsewhere: pages may take this one. */
export function isSwitcherHotkey(
  event: Pick<KeyboardEvent, "key" | "ctrlKey" | "altKey" | "metaKey" | "shiftKey">,
  mac: boolean,
): boolean {
  if (event.key.toLowerCase() !== "k" || event.altKey || event.shiftKey) return false;
  return mac ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
}

type RailGroup = RailRow["group"];

const GROUP_LABEL: Record<RailGroup, string> = {
  live: "Live now",
  attention: "Needs attention",
  recent: "Recent",
};

const LAMP_FOR_TONE: Record<RailActiveSession["tone"], StatusLampState> = {
  live: "working",
  attention: "waiting",
  unknown: "unknown",
  cool: "idle",
};

const TONE_FOR_LAMP: Record<StatusLampState, RailActiveSession["tone"]> = {
  working: "live",
  waiting: "attention",
  unknown: "unknown",
  idle: "cool",
  ended: "cool",
  done: "cool",
  failed: "attention",
};

function rowFromCard(
  card: TimelineSessionCard,
  group: RailGroup,
  nowMs: number,
): RailRow {
  const session = card.head;
  const status = getRowStatus({ thread: card, relativeNowMs: nowMs, unread: group === "attention" });
  return {
    id: session.id,
    title: getSessionCardText(session, { titleMaxChars: 96 }).title,
    provider: session.provider ?? null,
    host: session.control?.source_runner_name?.trim() || session.device_id || null,
    stateText: status.statusLabel,
    tone: TONE_FOR_LAMP[status.lampState],
    lamp: status.lampState,
    group,
  };
}

/**
 * The Timeline's own tiers, read with its own layout function so the rail and
 * the Timeline never disagree about what is live: Live now, results waiting
 * (Needs attention), then Recent. Automation runs (canaries, Hatch workers,
 * test launches), by the Timeline's own classifier, stay on the Timeline: the
 * rail is for sessions a person is steering.
 */
export function buildRailRows(
  cards: readonly TimelineSessionCard[],
  nowMs: number,
  active: RailActiveSession | null,
): RailRow[] {
  const people = cards.filter((card) => !isAutomationSession(card.head, getProjectLabel(card.head)));
  const layout = buildInboxLayout(people, undefined, nowMs);
  const recent = layout.history
    .flatMap((group) => group.sessions)
    .sort((a, b) => historySortKey(b) - historySortKey(a));
  const rows = [
    ...layout.shelf.map((card) => rowFromCard(card, "live", nowMs)),
    ...layout.unread.map((card) => rowFromCard(card, "attention", nowMs)),
    ...recent.map((card) => rowFromCard(card, "recent", nowMs)),
  ];
  if (!active) return rows;
  // The open session's own page has the freshest state; prefer its words.
  const activeRow = (group: RailGroup): RailRow => ({
    ...active,
    lamp: LAMP_FOR_TONE[active.tone],
    group,
  });
  const index = rows.findIndex((row) => row.id === active.id);
  if (index === -1) return [activeRow("recent"), ...rows];
  rows[index] = activeRow(rows[index].group);
  return rows;
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

function SessionRail({
  activeSessionId,
  activeSession,
  returnTo,
  onTurnsTarget,
  layout,
  onExpandedChange,
}: {
  activeSessionId: string | null;
  activeSession: RailActiveSession | null;
  returnTo: string;
  onTurnsTarget: (node: HTMLDivElement | null) => void;
  /** docked: beside the page. strip: a narrow column of glyphs that opens
   * over the page (laptop widths). drawer: inside the phone menu. */
  layout: "docked" | "strip" | "overlay" | "drawer";
  onExpandedChange?: (expanded: boolean) => void;
}) {
  const navigate = useNavigate();
  const nowMs = useWallClock(true);
  const mac = useMemo(isMacPlatform, []);
  const { data } = useAgentSessions(RAIL_SESSION_FILTERS, { refetchInterval: 30_000 });
  const [switcherOpen, setSwitcherOpenState] = useState(false);
  const switcherLabel = mac ? "⌘K" : "Ctrl+K";
  // Focus goes back where it was before the switcher opened (the composer,
  // a rail row), read before the switcher's own field takes it.
  const focusBeforeSwitcher = useRef<HTMLElement | null>(null);
  const switcherOpenRef = useRef(false);
  const setSwitcherOpen = useCallback((next: boolean | ((open: boolean) => boolean)) => {
    const open = switcherOpenRef.current;
    const value = typeof next === "function" ? next(open) : next;
    if (value === open) return;
    switcherOpenRef.current = value;
    if (value) {
      focusBeforeSwitcher.current =
        document.activeElement instanceof HTMLElement ? document.activeElement : null;
    } else {
      const target = focusBeforeSwitcher.current;
      focusBeforeSwitcher.current = null;
      queueMicrotask(() => {
        if (target?.isConnected) target.focus();
      });
    }
    setSwitcherOpenState(value);
  }, []);

  const rows = useMemo(
    () => buildRailRows(data?.sessions ?? [], nowMs, activeSession),
    [data?.sessions, nowMs, activeSession],
  );
  // Most sessions run on one machine; name the machine only on the rows that
  // run somewhere else, so the common case spends its width on the title.
  const usualHost = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of rows) if (row.host) counts.set(row.host, (counts.get(row.host) ?? 0) + 1);
    let best: string | null = null;
    for (const [host, count] of counts) if (best == null || count > (counts.get(best) ?? 0)) best = host;
    return best;
  }, [rows]);

  useRailPrefetch(
    useMemo(() => rows.map((row) => row.id), [rows]),
    activeSessionId,
  );

  const openSession = useCallback(
    (sessionId: string) => {
      if (sessionId === activeSessionId) return;
      navigate(`/timeline/${sessionId}`, { state: { from: returnTo } });
    },
    [activeSessionId, navigate, returnTo],
  );

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.repeat || event.isComposing) return;
      if (isSwitcherHotkey(event, mac)) {
        event.preventDefault();
        setSwitcherOpen((open) => !open);
        return;
      }
      if (switcherOpen) return;
      const index = railHotkeyIndex(event, mac);
      if (index == null || index >= Math.min(rows.length, RAIL_HOTKEY_COUNT)) return;
      // Alt+digit types characters on some layouts; never take it from a field.
      // Control+digit types nothing on a Mac, so it works from the composer too,
      // the way terminal tabs do.
      if (!mac && isEditableTarget(event.target)) return;
      event.preventDefault();
      openSession(rows[index].id);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [mac, openSession, rows, setSwitcherOpen, switcherOpen]);

  const strip = layout === "strip";
  const groups: RailGroup[] = ["live", "attention", "recent"];

  const renderRow = (row: RailRow, index: number) => {
    const active = row.id === activeSessionId;
    const hotkey = index < RAIL_HOTKEY_COUNT ? railHotkeyLabel(index, mac) : null;
    const host = row.host && row.host !== usualHost ? row.host : null;
    return (
      <li key={row.id}>
        <button
          type="button"
          className={`session-rail__row${active ? " is-active" : ""}`}
          aria-current={active ? "page" : undefined}
          aria-keyshortcuts={hotkey ? (mac ? `Control+${index + 1}` : `Alt+${index + 1}`) : undefined}
          aria-label={strip ? `${row.title}, ${row.stateText || "status unknown"}` : undefined}
          data-testid="session-rail-row"
          data-session-id={row.id}
          data-group={row.group}
          onClick={() => {
            openSession(row.id);
            if (layout === "overlay") onExpandedChange?.(false);
          }}
          title={[row.title, row.host, row.stateText, hotkey].filter(Boolean).join(" · ")}
        >
          <span className="session-rail__glyph">
            {row.provider ? <ProviderGlyph provider={row.provider} size={14} /> : null}
            {strip ? (
              <span className="session-rail__dot" data-state={row.lamp} aria-hidden="true" />
            ) : null}
          </span>
          {strip ? null : (
            <>
              <span className="session-rail__title">{row.title}</span>
              {host ? <span className="session-rail__host">{host}</span> : null}
              <span className="hearth-lamp session-rail__status" data-state={row.lamp}>
                <span className="session-rail__dot" data-state={row.lamp} aria-hidden="true" />
                <span className="hearth-lamp__label">{row.stateText}</span>
              </span>
            </>
          )}
        </button>
        {active && !strip ? <div className="session-rail__turns" ref={onTurnsTarget} /> : null}
      </li>
    );
  };

  return (
    <nav
      className={`session-rail session-rail--${layout}`}
      aria-label="Sessions"
      data-testid="session-rail"
    >
      <div className="session-rail__head">
        {strip ? (
          <button
            type="button"
            className="session-rail__toggle"
            onClick={() => onExpandedChange?.(true)}
            title="Show sessions"
            aria-label="Show sessions"
            aria-expanded={false}
            data-testid="session-rail-expand"
          >
            <span aria-hidden="true">»</span>
          </button>
        ) : (
          <>
            <span>Sessions</span>
            <span className="session-rail__head-actions">
              <button
                type="button"
                className="session-rail__find"
                onClick={() => setSwitcherOpen(true)}
                title={`Switch session (${switcherLabel})`}
                data-testid="session-switcher-open"
              >
                <SearchIcon width={12} height={12} />
                <span className="session-rail__find-key">{switcherLabel}</span>
                <span className="sr-only">Switch session</span>
              </button>
              <Link to={returnTo} className="session-rail__all">
                Timeline
              </Link>
              {layout === "overlay" ? (
                <button
                  type="button"
                  className="session-rail__toggle"
                  onClick={() => onExpandedChange?.(false)}
                  title="Hide sessions"
                  aria-label="Hide sessions"
                  aria-expanded
                  data-testid="session-rail-collapse"
                >
                  <XIcon width={12} height={12} />
                </button>
              ) : null}
            </span>
          </>
        )}
      </div>
      {switcherOpen
        ? createPortal(
            <SessionSwitcher
              rows={rows}
              activeSessionId={activeSessionId}
              shortcutLabel={switcherLabel}
              onClose={() => setSwitcherOpen(false)}
              onOpen={(sessionId) => {
                setSwitcherOpen(false);
                openSession(sessionId);
              }}
            />,
            document.body,
          )
        : null}
      {groups.map((group) => {
        const groupRows = rows
          .map((row, index) => ({ row, index }))
          .filter(({ row }) => row.group === group);
        if (groupRows.length === 0) return null;
        return (
          <section key={group} className="session-rail__group" aria-label={GROUP_LABEL[group]}>
            {strip ? null : (
              <h3 className="session-rail__group-label">
                {GROUP_LABEL[group]}
                <span className="session-rail__group-count">{groupRows.length}</span>
              </h3>
            )}
            <ol className="session-rail__list">{groupRows.map(({ row, index }) => renderRow(row, index))}</ol>
          </section>
        );
      })}
    </nav>
  );
}

/**
 * The persistent frame around the per-session route: the rail beside the
 * page on wide screens, inside the app's menu drawer on phones.
 */
export function SessionRailFrame({
  activeSessionId,
  returnTo,
  children,
}: {
  activeSessionId: string | null;
  returnTo: string;
  children: ReactNode;
}) {
  const narrow = useMediaQuery("(max-width: 767px)");
  // Below this the docked rail would squeeze the transcript: the rail
  // becomes a strip of glyphs that opens over the page instead.
  const compact = useMediaQuery("(max-width: 1199px)");
  const mobileSlot = useMobileNavSlot();
  const [turnsTarget, setTurnsTarget] = useState<HTMLElement | null>(null);
  const [activeSession, setActiveSession] = useState<RailActiveSession | null>(null);
  const [expanded, setExpanded] = useState(false);

  const reportActiveSession = useCallback((next: RailActiveSession | null) => {
    setActiveSession((previous) =>
      previous && next && JSON.stringify(previous) === JSON.stringify(next) ? previous : next,
    );
  }, []);

  useEffect(() => {
    if (!compact) setExpanded(false);
  }, [compact]);

  useEffect(() => {
    if (!expanded) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setExpanded(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [expanded]);

  const context = useMemo<SessionRailContextValue>(
    () => ({ turnsTarget, reportActiveSession }),
    [turnsTarget, reportActiveSession],
  );

  const railFor = (layout: "docked" | "strip" | "overlay" | "drawer") => (
    <SessionRail
      activeSessionId={activeSessionId}
      activeSession={activeSession?.id === activeSessionId ? activeSession : null}
      returnTo={returnTo}
      onTurnsTarget={setTurnsTarget}
      layout={layout}
      onExpandedChange={setExpanded}
    />
  );

  let rail: ReactNode;
  if (narrow) {
    rail = mobileSlot ? createPortal(railFor("drawer"), mobileSlot) : null;
  } else if (compact && expanded) {
    rail = (
      <>
        <button
          type="button"
          className="session-rail-frame__scrim"
          aria-label="Hide sessions"
          onClick={() => setExpanded(false)}
        />
        {railFor("overlay")}
      </>
    );
  } else {
    rail = railFor(compact ? "strip" : "docked");
  }

  return (
    <SessionRailContext.Provider value={context}>
      <div className={`session-rail-frame${compact && !narrow ? " session-rail-frame--compact" : ""}`}>
        {rail}
        <div className="session-rail-frame__main">{children}</div>
      </div>
    </SessionRailContext.Provider>
  );
}
