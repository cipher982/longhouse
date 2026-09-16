/**
 * Phase 4 (Instruments), web-restyle-signal.md. One isolated component +
 * the ".instrument-nixie" CSS block in styles/instruments.css — both
 * deletable together in one commit.
 *
 * A live quantity glows amber inside a glass capsule; a dead one (`dim`) is
 * plain-colored text with no glow. The glow is the information: this number
 * is changing right now. On a value change the digits flicker briefly unless
 * the caller opts out (elapsed clocks update every second and should stay
 * steady).
 */
import { useEffect, useRef, useState } from "react";

export interface NixieProps {
  value: string | number;
  /** Quantity that isn't changing right now — no glow, secondary ink. */
  dim?: boolean;
  className?: string;
  /** Full value for a tooltip when the capsule can visually truncate (e.g.
   * a long tool label in a fixed-width timeline row). */
  title?: string;
  /** Keep a frequently changing value readable instead of flashing on every update. */
  flickerOnChange?: boolean;
}

const FLICKER_MS = 140;

export function Nixie({
  value,
  dim = false,
  className,
  title,
  flickerOnChange = true,
}: NixieProps) {
  const [flicker, setFlicker] = useState(false);
  const previousValue = useRef(value);

  useEffect(() => {
    if (previousValue.current === value) return;
    previousValue.current = value;
    if (!flickerOnChange) return;
    setFlicker(true);
    const timer = window.setTimeout(() => setFlicker(false), FLICKER_MS);
    return () => window.clearTimeout(timer);
  }, [flickerOnChange, value]);

  const classes = ["instrument-nixie"];
  if (dim) classes.push("instrument-nixie--dim");
  if (flicker) classes.push("instrument-nixie--flicker");
  if (className) classes.push(className);

  return (
    <span className={classes.join(" ")} title={title}>
      {value}
    </span>
  );
}
