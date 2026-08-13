import { useEffect, useRef, useState } from "react";
import { TerminalGrid, type GridTimeline } from "@longhouse/video/demo";

const CELL_RATIO = 2.15;

export function RecordedSceneTerminal({
  replaySecond,
  timeline,
  sourceLabel,
}: {
  replaySecond: number;
  timeline: GridTimeline;
  sourceLabel: "recorded PTY" | "simulated continuation";
}) {
  const screenRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });

  useEffect(() => {
    const screen = screenRef.current;
    if (!screen) return;
    const observer = new ResizeObserver(([entry]) => {
      const width = Math.round(entry?.contentRect.width ?? 0);
      const height = Math.round(entry?.contentRect.height ?? 0);
      setSize((current) => current.width === width && current.height === height
        ? current
        : { width, height });
    });
    observer.observe(screen);
    return () => observer.disconnect();
  }, []);

  const { cols, rows } = timeline.meta;
  const cellWidth = size.width > 0 && size.height > 0
    ? Math.min(size.width / cols, size.height / (rows * CELL_RATIO))
    : 0;
  const cellHeight = cellWidth * CELL_RATIO;

  return (
    <div
      className="remote-scene-recorded-terminal remote-scene-recorded-terminal--cutin"
      aria-label={`Claude Code terminal, ${sourceLabel}`}
    >
      <div className="remote-scene-recorded-terminal-chrome">
        <span className="remote-scene-recorded-terminal-dots" aria-hidden="true"><i /><i /><i /></span>
        <strong>Claude Code</strong>
        <span>studio-mac · {sourceLabel}</span>
      </div>
      <div className="remote-scene-recorded-terminal-screen" ref={screenRef}>
        {cellWidth > 0 ? (
          <TerminalGrid
            timeline={timeline}
            tSec={replaySecond}
            cellW={cellWidth}
            cellH={cellHeight}
            background="#0b0908"
          />
        ) : null}
      </div>
    </div>
  );
}
