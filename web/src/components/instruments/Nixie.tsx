/**
 * Phase 4 (Instruments), web-restyle-signal.md. One isolated component +
 * the ".instrument-nixie" CSS block in styles/instruments.css — both
 * deletable together in one commit.
 *
 * A live quantity glows amber inside a glass capsule; a dead one (`dim`) is
 * plain-colored text with no glow. The glow is the information: this number
 * is changing right now. On a value change the digits flicker briefly
 * (disabled under prefers-reduced-motion via CSS, not JS).
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
}

const FLICKER_MS = 140;

export function Nixie({ value, dim = false, className, title }: NixieProps) {
  const [flicker, setFlicker] = useState(false);
  const previousValue = useRef(value);

  useEffect(() => {
    if (previousValue.current === value) return;
    previousValue.current = value;
    setFlicker(true);
    const timer = window.setTimeout(() => setFlicker(false), FLICKER_MS);
    return () => window.clearTimeout(timer);
  }, [value]);

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
