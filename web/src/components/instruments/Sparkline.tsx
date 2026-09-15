/**
 * Phase 4 (Instruments), web-restyle-signal.md. One isolated component +
 * the ".instrument-sparkline*" CSS block in styles/instruments.css — both
 * deletable together in one commit (along with activityBuckets.ts and
 * toolActivity.ts, which feed it data).
 *
 * An inline SVG polyline, no axes, no fill. Renders nothing (not an empty
 * box) when there is nothing to draw, so a quiet page never shows a bare
 * flat line pretending to be signal.
 */

const STROKE = "rgba(233, 185, 73, 0.75)";
const LIVE_DOT = "#F08A24";

export interface SparklineProps {
  /** Bucketed counts, oldest first (see activityBuckets.ts). */
  data: number[];
  width?: number;
  height?: number;
  /** Draws a flame dot at the last point — this activity is ongoing. */
  live?: boolean;
  className?: string;
  /** Accessible label; the SVG is otherwise decorative. */
  title?: string;
}

export function Sparkline({
  data,
  width = 140,
  height = 22,
  live = false,
  className,
  title,
}: SparklineProps) {
  const nonZeroBuckets = data.filter((value) => value > 0).length;
  if (nonZeroBuckets < 2) return null;

  const max = Math.max(...data, 1);
  const n = data.length;
  const step = n > 1 ? width / (n - 1) : width;
  const points = data
    .map((value, index) => {
      const x = index * step;
      const y = height - 2 - (value * (height - 4)) / max;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const lastY = height - 2 - (data[n - 1] * (height - 4)) / max;

  return (
    <svg
      className={["instrument-sparkline", className].filter(Boolean).join(" ")}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      fill="none"
      role={title ? "img" : undefined}
      aria-hidden={title ? undefined : true}
    >
      {title ? <title>{title}</title> : null}
      <polyline points={points} stroke={STROKE} strokeWidth={1} strokeLinejoin="round" />
      {live ? <circle cx={width} cy={lastY} r={2.2} fill={LIVE_DOT} /> : null}
    </svg>
  );
}
