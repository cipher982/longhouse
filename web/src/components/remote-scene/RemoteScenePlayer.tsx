import { useEffect, useMemo, useRef, useState, type ChangeEvent, type CSSProperties } from "react";
import { REMOTE_SCENE_DATA } from "./generated/sceneData";
import { clampFrameIndex, decodeFrames } from "./sceneCodec";
import { getPhoneSceneState, getSceneCamera, getTerminalSceneState } from "./sceneSpec";

const CELL_COUNT = REMOTE_SCENE_DATA.width * REMOTE_SCENE_DATA.height;

function formatTime(frameIndex: number): string {
  return (frameIndex / REMOTE_SCENE_DATA.fps).toFixed(2).padStart(5, "0");
}

function projectedPercent(value: number, axis: "x" | "y", frameIndex: number): number {
  const camera = getSceneCamera(frameIndex / REMOTE_SCENE_DATA.fps);
  if (axis === "x") return ((value - 50) * camera.scale + 50 + camera.lateral) / REMOTE_SCENE_DATA.width * 100;
  return (camera.horizon + (value - camera.horizon) * camera.scale) / REMOTE_SCENE_DATA.height * 100;
}

function drawFrame(canvas: HTMLCanvasElement, frame: Uint8Array): void {
  const bounds = canvas.getBoundingClientRect();
  const pixelRatio = window.devicePixelRatio || 1;
  const pixelWidth = Math.max(1, Math.round(bounds.width * pixelRatio));
  const pixelHeight = Math.max(1, Math.round(bounds.height * pixelRatio));
  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
  }

  const context = canvas.getContext("2d");
  if (!context) return;
  context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  context.clearRect(0, 0, bounds.width, bounds.height);
  context.fillStyle = REMOTE_SCENE_DATA.palette[0].color;
  context.fillRect(0, 0, bounds.width, bounds.height);

  const cellWidth = bounds.width / REMOTE_SCENE_DATA.width;
  const cellHeight = bounds.height / REMOTE_SCENE_DATA.height;
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.font = `${Math.max(6, Math.min(cellWidth * 1.08, cellHeight * 1.2))}px "JetBrains Mono", monospace`;

  let tone = -1;
  for (let index = 0; index < frame.length; index += 1) {
    const nextTone = frame[index];
    if (nextTone === 0) continue;
    if (nextTone !== tone) {
      context.fillStyle = REMOTE_SCENE_DATA.palette[nextTone].color;
      tone = nextTone;
    }
    const column = index % REMOTE_SCENE_DATA.width;
    const row = Math.floor(index / REMOTE_SCENE_DATA.width);
    context.fillText(
      REMOTE_SCENE_DATA.palette[nextTone].glyph,
      column * cellWidth + cellWidth / 2,
      row * cellHeight + cellHeight / 2,
    );
  }
}

export function RemoteScenePlayer() {
  const frames = useMemo(
    () => decodeFrames(REMOTE_SCENE_DATA.encodedFrames, CELL_COUNT),
    [REMOTE_SCENE_DATA.encodedFrames],
  );
  const lastFrame = Math.max(0, frames.length - 1);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const frameIndexRef = useRef(0);
  const [frameIndex, setFrameIndex] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [staticPreview, setStaticPreview] = useState(false);
  const [prefersReducedMotion, setPrefersReducedMotion] = useState(false);

  useEffect(() => {
    const mediaQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    const updatePreference = () => setPrefersReducedMotion(mediaQuery.matches);
    updatePreference();
    mediaQuery.addEventListener("change", updatePreference);
    return () => mediaQuery.removeEventListener("change", updatePreference);
  }, []);

  useEffect(() => {
    if (prefersReducedMotion) {
      setStaticPreview(true);
      setIsPlaying(false);
    }
  }, [prefersReducedMotion]);

  useEffect(() => {
    const safeFrameIndex = clampFrameIndex(frameIndex, frames.length);
    frameIndexRef.current = safeFrameIndex;
    if (safeFrameIndex !== frameIndex) setFrameIndex(safeFrameIndex);
    const canvas = canvasRef.current;
    if (canvas) drawFrame(canvas, frames[safeFrameIndex]);
  }, [frameIndex, frames]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const redraw = () => {
      const safeFrameIndex = clampFrameIndex(frameIndexRef.current, frames.length);
      frameIndexRef.current = safeFrameIndex;
      drawFrame(canvas, frames[safeFrameIndex]);
    };
    const observer = new ResizeObserver(redraw);
    observer.observe(canvas);
    redraw();
    return () => observer.disconnect();
  }, [frames]);

  useEffect(() => {
    if (!isPlaying || staticPreview) return;
    const startedAt = performance.now() - frameIndexRef.current * (1000 / REMOTE_SCENE_DATA.fps);
    let animationFrame = 0;
    const tick = (now: number) => {
      const nextFrame = Math.min(lastFrame, Math.floor((now - startedAt) / (1000 / REMOTE_SCENE_DATA.fps)));
      frameIndexRef.current = nextFrame;
      setFrameIndex(nextFrame);
      if (nextFrame >= lastFrame) {
        setIsPlaying(false);
      } else {
        animationFrame = requestAnimationFrame(tick);
      }
    };
    animationFrame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(animationFrame);
  }, [isPlaying, lastFrame, staticPreview]);

  const safeFrameIndex = clampFrameIndex(frameIndex, frames.length);
  const camera = getSceneCamera(safeFrameIndex / REMOTE_SCENE_DATA.fps);
  const terminalState = getTerminalSceneState(safeFrameIndex);
  const phoneState = getPhoneSceneState(safeFrameIndex);
  const workProgress = Math.max(0, Math.min(1, (safeFrameIndex - 18) / (56 - 18)));
  const overlayStyle = {
    "--monitor-left": `${projectedPercent(56, "x", safeFrameIndex)}%`,
    "--monitor-top": `${projectedPercent(15, "y", safeFrameIndex)}%`,
    "--monitor-width": `${24 * camera.scale}%`,
    "--monitor-height": `${14.2 * camera.scale}%`,
    "--phone-left": `${projectedPercent(73.4, "x", safeFrameIndex)}%`,
    "--phone-top": `${projectedPercent(35, "y", safeFrameIndex)}%`,
    "--phone-width": `${12.6 * camera.scale}%`,
    "--phone-height": `${18.2 * camera.scale}%`,
    "--work-progress": workProgress,
  } as CSSProperties;

  const setFrame = (nextFrame: number) => {
    const safeNextFrame = clampFrameIndex(nextFrame, frames.length);
    frameIndexRef.current = safeNextFrame;
    setFrameIndex(safeNextFrame);
  };

  const handleScrub = (event: ChangeEvent<HTMLInputElement>) => {
    setIsPlaying(false);
    setFrame(Number(event.target.value));
  };

  const toggleStaticPreview = () => {
    const nextStatic = !staticPreview;
    setStaticPreview(nextStatic);
    if (nextStatic) setIsPlaying(false);
  };

  return (
    <section className="remote-scene-player" aria-label="Remote control scene player">
      <div className={`remote-scene-stage${safeFrameIndex >= 56 ? " remote-scene-stage--complete" : ""}`} style={overlayStyle}>
        <div className="remote-scene-world">
          <canvas ref={canvasRef} aria-hidden="true" />
          <div className="remote-scene-terminal-overlay" aria-label="Crisp workstation status overlay">
            <div className="remote-scene-terminal-heading">
              <span><i /> studio-mac</span>
              <strong>{terminalState.status}</strong>
            </div>
            <div className="remote-scene-terminal-rule" />
            {terminalState.lines.map((line, index) => (
              <span
                className={`remote-scene-terminal-line${index === 0 ? " remote-scene-output-accent" : ""}`}
                key={`${terminalState.key}-${line}`}
              >
                {line}
              </span>
            ))}
            <div className="remote-scene-work-progress"><i /></div>
          </div>
          <div className={`remote-scene-phone-overlay remote-scene-phone-overlay--${phoneState.key}`} aria-label="Crisp phone status overlay">
            <div className="remote-scene-phone-heading"><strong>Longhouse</strong><small>studio-mac</small></div>
            <span className="remote-scene-phone-live">● {phoneState.status}</span>
            <span className="remote-scene-phone-message">{phoneState.message}</span>
            <span className="remote-scene-phone-detail">{phoneState.detail}</span>
            <div className="remote-scene-work-progress"><i /></div>
            <b>›</b>
          </div>
          <div className="remote-scene-ambient-label">
            {safeFrameIndex < 38
              ? "the machine stays awake"
              : safeFrameIndex < 56 ? "work continues on studio-mac" : "task finished while you were away"}
          </div>
        </div>
      </div>

      <div className="remote-scene-controls">
        <div className="remote-scene-control-row">
          <button
            type="button"
            className="remote-scene-play-button"
            onClick={() => {
              if (safeFrameIndex >= lastFrame) setFrame(0);
              setIsPlaying((playing) => !playing);
            }}
            aria-label={isPlaying ? "Pause scene" : "Play scene"}
          >
            {isPlaying ? "Pause" : "Play scene"}
          </button>
          <span className="remote-scene-time" aria-live="off">
            {formatTime(safeFrameIndex)} <span>/</span> {formatTime(lastFrame)}
          </span>
          <button
            type="button"
            className="remote-scene-static-button"
            onClick={toggleStaticPreview}
            aria-pressed={staticPreview}
          >
            {staticPreview ? "Motion preview" : "Static frame"}
          </button>
        </div>
        <label className="remote-scene-scrub-label">
          <span className="visually-hidden">Scene position</span>
          <input
            type="range"
            min="0"
            max={lastFrame}
            step="1"
            value={safeFrameIndex}
            onChange={handleScrub}
            aria-label="Scrub remote control scene"
          />
        </label>
        <div className="remote-scene-control-note">
          <span>{prefersReducedMotion ? "Reduced motion preference detected" : "12 fps deterministic preview"}</span>
          <span>Frame {String(safeFrameIndex).padStart(2, "0")} / {lastFrame}</span>
        </div>
      </div>
    </section>
  );
}
