import { useEffect, useMemo, useRef, useState, type ChangeEvent, type CSSProperties } from "react";
import { REMOTE_SCENE_DATA } from "./generated/sceneData";
import { decodeFrames } from "./sceneCodec";
import { getSceneCamera, PHONE_LINES, TERMINAL_LINES } from "./sceneSpec";

const CELL_COUNT = REMOTE_SCENE_DATA.width * REMOTE_SCENE_DATA.height;
const LAST_FRAME = Math.max(0, REMOTE_SCENE_DATA.fps * REMOTE_SCENE_DATA.durationSeconds - 1);

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
    [],
  );
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
    frameIndexRef.current = frameIndex;
    const canvas = canvasRef.current;
    if (canvas) drawFrame(canvas, frames[frameIndex]);
  }, [frameIndex, frames]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const redraw = () => drawFrame(canvas, frames[frameIndexRef.current]);
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
      const nextFrame = Math.min(LAST_FRAME, Math.floor((now - startedAt) / (1000 / REMOTE_SCENE_DATA.fps)));
      frameIndexRef.current = nextFrame;
      setFrameIndex(nextFrame);
      if (nextFrame >= LAST_FRAME) {
        setIsPlaying(false);
      } else {
        animationFrame = requestAnimationFrame(tick);
      }
    };
    animationFrame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(animationFrame);
  }, [isPlaying, staticPreview]);

  const camera = getSceneCamera(frameIndex / REMOTE_SCENE_DATA.fps);
  const overlayStyle = {
    "--monitor-left": `${projectedPercent(56, "x", frameIndex)}%`,
    "--monitor-top": `${projectedPercent(15, "y", frameIndex)}%`,
    "--monitor-width": `${21 * camera.scale}%`,
    "--monitor-height": `${12 * camera.scale}%`,
    "--phone-left": `${projectedPercent(80.5, "x", frameIndex)}%`,
    "--phone-top": `${projectedPercent(39.5, "y", frameIndex)}%`,
    "--phone-width": `${8.2 * camera.scale}%`,
    "--phone-height": `${14.6 * camera.scale}%`,
  } as CSSProperties;

  const setFrame = (nextFrame: number) => {
    frameIndexRef.current = nextFrame;
    setFrameIndex(nextFrame);
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
      <div className="remote-scene-stage" style={overlayStyle}>
        <canvas ref={canvasRef} aria-hidden="true" />
        <div className="remote-scene-terminal-overlay" aria-label="Crisp workstation status overlay">
          <div className="remote-scene-terminal-heading">
            <span><i /> studio-mac</span>
            <strong>AWAKE</strong>
          </div>
          <div className="remote-scene-terminal-rule" />
          {TERMINAL_LINES.map((line) => <span key={line}>{line}</span>)}
          <span className="remote-scene-terminal-caret">▌</span>
        </div>
        <div className="remote-scene-phone-overlay" aria-label="Crisp phone status overlay">
          <strong>{PHONE_LINES[0]}</strong>
          <span className="remote-scene-phone-live">● {PHONE_LINES[1]}</span>
          <span>{PHONE_LINES[2]}</span>
          <b>›</b>
        </div>
        <div className="remote-scene-ambient-label">the machine stays awake</div>
      </div>

      <div className="remote-scene-controls">
        <div className="remote-scene-control-row">
          <button
            type="button"
            className="remote-scene-play-button"
            onClick={() => {
              if (frameIndex >= LAST_FRAME) setFrame(0);
              setIsPlaying((playing) => !playing);
            }}
            aria-label={isPlaying ? "Pause scene" : "Play scene"}
          >
            {isPlaying ? "Pause" : "Play scene"}
          </button>
          <span className="remote-scene-time" aria-live="off">
            {formatTime(frameIndex)} <span>/</span> {formatTime(LAST_FRAME)}
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
            max={LAST_FRAME}
            step="1"
            value={frameIndex}
            onChange={handleScrub}
            aria-label="Scrub remote control scene"
          />
        </label>
        <div className="remote-scene-control-note">
          <span>{prefersReducedMotion ? "Reduced motion preference detected" : "12 fps deterministic preview"}</span>
          <span>Frame {String(frameIndex).padStart(2, "0")} / {LAST_FRAME}</span>
        </div>
      </div>
    </section>
  );
}
