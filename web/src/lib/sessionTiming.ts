import type { AgentSession, AgentSessionTurn } from "../services/api/agents";
import { parseUTC } from "./dateUtils";
import { isSessionClosed } from "./sessionRuntime";

const LIVE_TURN_STATES = new Set(["created", "send_accepted", "active"]);

function toEpochMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const epochMs = parseUTC(value).getTime();
  return Number.isFinite(epochMs) ? epochMs : null;
}

export function getActiveSessionTurn(
  turns: AgentSessionTurn[],
): AgentSessionTurn | null {
  return turns.find((turn) => LIVE_TURN_STATES.has(turn.state)) ?? null;
}

export function formatElapsedCounter(
  startedAt: string | null | undefined,
  endedAt: string | null | undefined,
  nowMs: number,
): string | null {
  const startMs = toEpochMs(startedAt);
  if (startMs == null) return null;

  const endMs = toEpochMs(endedAt) ?? nowMs;
  const elapsedSeconds = Math.max(0, Math.floor((endMs - startMs) / 1000));
  const hours = Math.floor(elapsedSeconds / 3600);
  const minutes = Math.floor((elapsedSeconds % 3600) / 60);
  const seconds = elapsedSeconds % 60;

  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

export function getRuntimeElapsedLabel(
  session: AgentSession | null,
  turns: AgentSessionTurn[],
  nowMs: number,
): string | null {
  if (!session) return null;

  const activeTurn = getActiveSessionTurn(turns);
  if (activeTurn) {
    const turnCounter = formatElapsedCounter(
      activeTurn.user_submitted_at,
      activeTurn.terminal_at ?? activeTurn.durable_at,
      nowMs,
    );
    return turnCounter ? `Turn ${turnCounter}` : null;
  }

  const endedAt = isSessionClosed(session) ? session.ended_at : null;
  const sessionCounter = formatElapsedCounter(
    session.started_at,
    endedAt,
    nowMs,
  );
  return sessionCounter ? `Session ${sessionCounter}` : null;
}
