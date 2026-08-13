import { useEffect, useRef, useState } from "react";
import { TerminalGrid } from "@longhouse/video/demo";
import { REMOTE_SCENE_RECORDING } from "./recordedTimeline";

export type RecordedTerminalMode = "inset" | "cutin";

const CELL_RATIO = 2.15;

export function RecordedSceneTerminal({
  mode,
  replaySecond,
}: {
  mode: RecordedTerminalMode;
  replaySecond: number;
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

  const { cols, rows } = REMOTE_SCENE_RECORDING.meta;
  const cellWidth = size.width > 0 && size.height > 0
    ? Math.min(size.width / cols, size.height / (rows * CELL_RATIO))
    : 0;
  const cellHeight = cellWidth * CELL_RATIO;

  return (
    <div
      className={`remote-scene-recorded-terminal remote-scene-recorded-terminal--${mode}`}
      aria-label="Real recorded Claude Code terminal"
    >
      <div className="remote-scene-recorded-terminal-chrome">
        <span className="remote-scene-recorded-terminal-dots" aria-hidden="true"><i /><i /><i /></span>
        <strong>Claude Code</strong>
        <span>studio-mac · recorded PTY</span>
      </div>
      <div className="remote-scene-recorded-terminal-screen" ref={screenRef}>
        {cellWidth > 0 ? (
          <TerminalGrid
            timeline={REMOTE_SCENE_RECORDING}
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
