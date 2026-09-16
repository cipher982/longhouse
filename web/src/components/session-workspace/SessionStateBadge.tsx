import type { SessionHeaderStateTone } from "./sessionHeaderState";

/**
 * Dot + sentence state readout, shared by the session header and the
 * composer header. Live breathes (ember), attention is a static red ember,
 * unknown is a static outlined marker, and cool is a static ash dot (idle or
 * ended).
 */
export function SessionStateBadge({
  tone,
  text,
  testId,
}: {
  tone: SessionHeaderStateTone;
  text: string;
  testId?: string;
}) {
  return (
    <span className="session-state-line" data-tone={tone} data-testid={testId} title={text}>
      <span
        className={
          tone === "live"
            ? "session-ember-dot"
            : tone === "attention"
              ? "session-ember-dot session-ember-dot--attention"
              : tone === "unknown"
                ? "session-unknown-dot"
                : "session-cool-dot"
        }
        aria-hidden="true"
      />
      <span className="session-state-line__text">{text}</span>
    </span>
  );
}
