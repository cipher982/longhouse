/**
 * StatusLamp — the one "is the agent doing anything?" instrument, shared by
 * the timeline row, the session header, and (bulb only) the composer head.
 * CSS lives in the ".status-lamp" block of styles/instruments.css.
 *
 * Same geometry in every state — a bulb then a label, at the same insets —
 * so a row never swaps one kind of object for another as a session moves
 * between states. State is carried by treatment, never by hue alone:
 *
 *   working  lit bulb with a halo that breathes, inside a lit glass capsule
 *   waiting  steady bulb inside a filled ember capsule (the one loud state)
 *   idle     solid ash bulb, no capsule
 *   unknown  hollow ring: no current evidence either way
 *   ended    a flat line, the most receded
 *   done     solid sage bulb (an unread Console result that finished)
 *   failed   solid ember bulb, ember label, no capsule (a fact, not a request)
 *
 * The label is always visible text, so the state reaches a screen reader
 * and a colour-blind reader without relying on the bulb.
 */

export type StatusLampState =
  | "working"
  | "waiting"
  | "idle"
  | "unknown"
  | "ended"
  | "done"
  | "failed";

export function StatusLamp({
  state,
  label,
  title,
  className,
  testId,
}: {
  state: StatusLampState;
  label: string;
  /** Full text for a tooltip when the label can truncate. */
  title?: string;
  className?: string;
  testId?: string;
}) {
  return (
    <span
      className={className ? `status-lamp ${className}` : "status-lamp"}
      data-state={state}
      data-testid={testId}
      title={title ?? label}
    >
      <StatusBulb state={state} />
      <span className="status-lamp__label">{label}</span>
    </span>
  );
}

/** The bulb alone, for a surface that sets its own label (composer head). */
export function StatusBulb({ state }: { state: StatusLampState }) {
  return <span className="status-lamp__bulb" data-state={state} aria-hidden="true" />;
}
