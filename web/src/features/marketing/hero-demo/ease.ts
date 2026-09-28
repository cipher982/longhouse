/** Tiny time-based easing helpers for clock-driven animation. */

export const clamp01 = (v: number): number => Math.min(1, Math.max(0, v));

export const clamp = (v: number, lo: number, hi: number): number =>
  Math.min(hi, Math.max(lo, v));

export const easeOutCubic = (p: number): number => 1 - Math.pow(1 - clamp01(p), 3);

/** 0→1 eased ramp starting at `startSec`, lasting `durSec`. */
export const ramp = (tSec: number, startSec: number, durSec = 0.4): number =>
  easeOutCubic((tSec - startSec) / durSec);
