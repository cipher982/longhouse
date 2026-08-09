import { memo, useEffect, useRef, useState } from "react";
import { TerminalGrid, type GridTimeline } from "@longhouse/video/demo";

/**
 * A recorded terminal that sizes itself to its container: cell metrics
 * derive from the measured width, so the type renders at real pixel size
 * on every screen instead of uniform-scaling a fixed-width canvas.
 *
 * Geometry is fixed at record time (cols x rows); only cell size flexes.
 */

/** Cell height : width ratio matching the recordings' terminal metrics. */
const CELL_RATIO = 2.15;

function TerminalWindow({
  timeline,
  tSec,
  title,
  accent,
  detail,
}: {
  timeline: GridTimeline;
  tSec: number;
  title: string;
  accent: string;
  detail?: string;
}) {
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const body = bodyRef.current;
    if (!body || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      const next = Math.round(entry?.contentRect.width ?? 0);
      setWidth((prev) => (prev === next ? prev : next));
    });
    observer.observe(body);
    return () => observer.disconnect();
  }, []);

  const { cols, rows } = timeline.meta;
  const cellW = width > 0 ? width / cols : 0;

  return (
    <div className="hero-demo-terminal">
      <div className="hero-demo-terminal-chrome">
        <span className="hero-demo-terminal-dots" aria-hidden="true">
          <i /><i /><i />
        </span>
        <span className="hero-demo-terminal-title" style={{ color: accent }}>
          {title}
        </span>
        {detail ? <span className="hero-demo-terminal-detail">{detail}</span> : null}
      </div>
      <div className="hero-demo-terminal-body">
        <div
          ref={bodyRef}
          className="hero-demo-terminal-screen"
          style={{ aspectRatio: `${cols} / ${(rows * CELL_RATIO).toFixed(3)}` }}
        >
          {cellW > 0 ? (
            <TerminalGrid
              timeline={timeline}
              tSec={tSec}
              cellW={cellW}
              cellH={cellW * CELL_RATIO}
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}

export const ResponsiveTerminal = memo(TerminalWindow);
