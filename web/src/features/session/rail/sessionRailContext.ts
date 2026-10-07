import { createContext, useContext, useEffect } from "react";
import type { StatusLampState } from "@/shared/instruments/StatusLamp";
import type { HearthSnapshot } from "@/shared/instruments/hearth/signals";

/** What the open session tells the rail about itself, so the rail can show
 * it even when it is not among the recent sessions the rail lists. */
export interface RailActiveSession {
  id: string;
  title: string;
  provider: string | null;
  host: string | null;
  stateText: string;
  tone: "live" | "attention" | "unknown" | "cool";
  statusKey?: string | null;
  statusTone?: string;
  /** True only for a current answer/approval interaction. */
  needsUser?: boolean;
}

/** One rail row: the session, its Timeline tier, and the Timeline's lamp. */
export type RailRow = RailActiveSession & {
  lamp: StatusLampState;
  group: "live" | "attention" | "recent";
  /** The row's fire, from the listed session's own counters; absent for an
   * open session the list does not carry, which keeps the plain dot. */
  hearth?: HearthSnapshot;
  /** The second line under a live or waiting row: the server's status label
   * ("Using Bash", "Needs your answer"). Only the open session adds a
   * duration, from its page's turn clock; a listed row has no turn start, and
   * the activity heartbeat is a different clock that would disagree. */
  detail?: string;
};

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
