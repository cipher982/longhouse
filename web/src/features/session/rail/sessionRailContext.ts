import { createContext, useContext, useEffect } from "react";

/** What the open session tells the rail about itself, so the rail can show
 * it even when it is not among the recent sessions the rail lists. */
export interface RailActiveSession {
  id: string;
  title: string;
  provider: string | null;
  host: string | null;
  stateText: string;
  tone: "live" | "attention" | "unknown" | "cool";
}

export interface SessionRailContextValue {
  /** Where the open session's turns list renders: inside its rail row. */
  turnsTarget: HTMLElement | null;
  reportActiveSession: (session: RailActiveSession | null) => void;
}

/** `null` outside the rail frame (tests, shared views): the page then keeps
 * its own turns column. */
export const SessionRailContext = createContext<SessionRailContextValue | null>(null);

export function useSessionRail(): SessionRailContextValue | null {
  return useContext(SessionRailContext);
}

/** The open session's page tells the rail what it looks like right now. */
export function useReportActiveSession(session: RailActiveSession | null) {
  const report = useContext(SessionRailContext)?.reportActiveSession;
  useEffect(() => {
    report?.(session);
  }, [report, session]);
}
