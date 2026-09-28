import type { SessionHeaderStateTone } from "./sessionHeaderState";
import { StatusLamp, type StatusLampState } from "@/shared/instruments/StatusLamp";

/**
 * The session header's state readout: the same StatusLamp the timeline row
 * uses, so a session looks the same in the list and once opened. Live is the
 * lit lamp, attention the filled one, unknown the hollow ring, and cool is the
 * ash bulb while idle or the flat line once the session has ended.
 */
export function headerLampState(tone: SessionHeaderStateTone, ended: boolean): StatusLampState {
  switch (tone) {
    case "live":
      return "working";
    case "attention":
      return "waiting";
    case "unknown":
      return "unknown";
    default:
      return ended ? "ended" : "idle";
  }
}

export function SessionStateBadge({
  tone,
  text,
  ended = false,
  testId,
}: {
  tone: SessionHeaderStateTone;
  text: string;
  /** The session is closed: a cool tone reads as ended, not idle. */
  ended?: boolean;
  testId?: string;
}) {
  return (
    <span className="session-state-line" data-tone={tone} data-testid={testId}>
      <StatusLamp state={headerLampState(tone, ended)} label={text} />
    </span>
  );
}
