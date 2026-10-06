/**
 * Session rail (session-view-terminal-parity C8, flavor A): recent sessions
 * down the left of the session view, each one keystroke away, with the open
 * session expanded into its turns. It lives outside the per-session route,
 * so switching sessions swaps the transcript without remounting the rail.
 *
 * Rows come from the timeline's own first page (the same query and cache
 * entry the timeline uses), so the rail adds no endpoint and no extra fetch
 * when the user arrives from the timeline.
 */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Link, useNavigate } from "react-router";
import type { AgentSession } from "@/shared/api/agents";
import { useAgentSessions } from "@/shared/api/useAgentSessions";
import { useMobileNavSlot } from "@/app/headerSlot";
import { useMediaQuery } from "@/shared/hooks/useMediaQuery";
import { useWallClock } from "@/shared/hooks/useWallClock";
import { getSessionCardText } from "@/shared/session/sessionLabels";
import { ProviderGlyph } from "@/shared/ui/ProviderGlyph";
import { SearchIcon } from "@/shared/ui/icons";
import { getSessionHeaderState } from "../sessionHeaderState";
import {
  SessionRailContext,
  type RailActiveSession,
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

type RailRow = {
  id: string;
  title: string;
  provider: string | null;
  host: string | null;
  stateText: string;
  tone: RailActiveSession["tone"];
};

function rowFromSession(session: AgentSession, nowMs: number): RailRow {
  const state = getSessionHeaderState(session, nowMs);
  return {
    id: session.id,
    title: getSessionCardText(session, { titleMaxChars: 80 }).title,
    provider: session.provider ?? null,
    host: session.control?.source_runner_name?.trim() || session.device_id || null,
    stateText: state.text,
    tone: state.tone,
  };
}

function SessionRail({
  activeSessionId,
  activeSession,
  returnTo,
  onTurnsTarget,
}: {
  activeSessionId: string | null;
  activeSession: RailActiveSession | null;
  returnTo: string;
  onTurnsTarget: (node: HTMLDivElement | null) => void;
}) {
  const navigate = useNavigate();
  const nowMs = useWallClock(true);
  const mac = useMemo(isMacPlatform, []);
  const { data } = useAgentSessions(RAIL_SESSION_FILTERS, { refetchInterval: 30_000 });
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const switcherLabel = mac ? "⌘K" : "Ctrl+K";

  const rows = useMemo(() => {
    const listed = (data?.sessions ?? []).map((card) => rowFromSession(card.head, nowMs));
    if (activeSession && !listed.some((row) => row.id === activeSession.id)) {
      return [activeSession, ...listed];
    }
    // The open session's own page has the freshest state; prefer it.
    return listed.map((row) => (activeSession && row.id === activeSession.id ? activeSession : row));
  }, [data?.sessions, nowMs, activeSession]);

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
      if (isSwitcherHotkey(event, mac)) {
        event.preventDefault();
        setSwitcherOpen((open) => !open);
        return;
      }
      const index = railHotkeyIndex(event, mac);
      if (index == null || index >= Math.min(rows.length, RAIL_HOTKEY_COUNT)) return;
      event.preventDefault();
      openSession(rows[index].id);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [mac, openSession, rows]);

  return (
    <nav className="session-rail" aria-label="Sessions" data-testid="session-rail">
      <div className="session-rail__head">
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
            {switcherLabel}
          </button>
          <Link to={returnTo} className="session-rail__all">
            Timeline
          </Link>
        </span>
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
      <ol className="session-rail__list">
        {rows.map((row, index) => {
          const active = row.id === activeSessionId;
          return (
            <li key={row.id}>
              <button
                type="button"
                className={`session-rail__row${active ? " is-active" : ""}`}
                aria-current={active ? "page" : undefined}
                data-testid="session-rail-row"
                data-session-id={row.id}
                onClick={() => openSession(row.id)}
                title={row.title}
              >
                {row.provider ? (
                  <ProviderGlyph provider={row.provider} size={14} className="session-rail__glyph" />
                ) : (
                  <span className="session-rail__glyph" aria-hidden="true" />
                )}
                <span className="session-rail__title">{row.title}</span>
                <span className="session-rail__key">
                  <span className={`session-rail__dot session-rail__dot--${row.tone}`} aria-hidden="true" />
                  {index < RAIL_HOTKEY_COUNT ? railHotkeyLabel(index, mac) : null}
                </span>
                <span className="session-rail__sub">
                  {[row.host, row.stateText].filter(Boolean).join(" · ")}
                </span>
              </button>
              {active ? <div className="session-rail__turns" ref={onTurnsTarget} /> : null}
            </li>
          );
        })}
      </ol>
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
  const mobileSlot = useMobileNavSlot();
  const [turnsTarget, setTurnsTarget] = useState<HTMLElement | null>(null);
  const [activeSession, setActiveSession] = useState<RailActiveSession | null>(null);

  const reportActiveSession = useCallback((next: RailActiveSession | null) => {
    setActiveSession((previous) =>
      previous && next && JSON.stringify(previous) === JSON.stringify(next) ? previous : next,
    );
  }, []);

  const context = useMemo<SessionRailContextValue>(
    () => ({ turnsTarget, reportActiveSession }),
    [turnsTarget, reportActiveSession],
  );

  const rail = (
    <SessionRail
      activeSessionId={activeSessionId}
      activeSession={activeSession?.id === activeSessionId ? activeSession : null}
      returnTo={returnTo}
      onTurnsTarget={setTurnsTarget}
    />
  );

  return (
    <SessionRailContext.Provider value={context}>
      <div className="session-rail-frame">
        {narrow ? (mobileSlot ? createPortal(rail, mobileSlot) : null) : rail}
        <div className="session-rail-frame__main">{children}</div>
      </div>
    </SessionRailContext.Provider>
  );
}
