import type { SessionHeaderStateTone } from "./sessionHeaderState";

/**
 * Dot + sentence state readout, shared by the session header and the
 * composer header. Three tones only: live breathes (ember, flame-colored),
 * attention is a static ember (a question is pending), cool is a static
 * ash dot (idle or ended) — the one cold foil in the palette.
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
              : "session-cool-dot"
        }
        aria-hidden="true"
      />
      <span className="session-state-line__text">{text}</span>
    </span>
  );
}
