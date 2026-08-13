import { useEffect, useMemo, useRef, useState, type ChangeEvent, type CSSProperties } from "react";
import { REMOTE_SCENE_DATA } from "./generated/sceneData";
import { clampFrameIndex, decodeFrames } from "./sceneCodec";
import {
  getPhoneSceneState,
  getSceneCaption,
  getSceneOverlayLayout,
  getTerminalSceneState,
  type SceneProfileKey,
} from "./sceneSpec";

function formatTime(frameIndex: number): string {
  return (frameIndex / REMOTE_SCENE_DATA.fps).toFixed(2).padStart(5, "0");
}

function drawFrame(
  canvas: HTMLCanvasElement,
  frame: Uint8Array,
  width: number,
  height: number,
): void {
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

  const cellWidth = bounds.width / width;
  const cellHeight = bounds.height / height;
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.font = `${Math.max(5, Math.min(cellWidth * 1.08, cellHeight * 1.18))}px "JetBrains Mono", monospace`;

  let paletteIndex = -1;
  for (let index = 0; index < frame.length; index += 1) {
    const nextPaletteIndex = frame[index];
    if (nextPaletteIndex === 0) continue;
    if (nextPaletteIndex !== paletteIndex) {
      context.fillStyle = REMOTE_SCENE_DATA.palette[nextPaletteIndex].color;
      paletteIndex = nextPaletteIndex;
    }
    const column = index % width;
    const row = Math.floor(index / width);
    context.fillText(
      REMOTE_SCENE_DATA.palette[nextPaletteIndex].glyph,
      column * cellWidth + cellWidth / 2,
      row * cellHeight + cellHeight / 2,
    );
  }
}

export function RemoteScenePlayer() {
  const playerRef = useRef<HTMLElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const frameIndexRef = useRef(0);
  const [profileKey, setProfileKey] = useState<SceneProfileKey>(() =>
    typeof window !== "undefined" && window.matchMedia("(max-width: 720px)").matches ? "mobile" : "desktop",
  );
  const [frameIndex, setFrameIndex] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [staticPreview, setStaticPreview] = useState(false);
  const [prefersReducedMotion, setPrefersReducedMotion] = useState(false);
  const [isVisible, setIsVisible] = useState(true);

  const profile = REMOTE_SCENE_DATA.profiles[profileKey];
  const frames = useMemo(
    () => decodeFrames(profile.encodedFrames, profile.width * profile.height),
    [profile.encodedFrames, profile.height, profile.width],
  );
  const lastFrame = Math.max(0, frames.length - 1);

  useEffect(() => {
    const mediaQuery = window.matchMedia("(max-width: 720px)");
    const updateProfile = () => setProfileKey(mediaQuery.matches ? "mobile" : "desktop");
    updateProfile();
    mediaQuery.addEventListener("change", updateProfile);
    return () => mediaQuery.removeEventListener("change", updateProfile);
  }, []);

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
    const player = playerRef.current;
    if (!player) return;
    const observer = new IntersectionObserver(([entry]) => setIsVisible(entry.isIntersecting), { threshold: 0.08 });
    observer.observe(player);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const handleVisibility = () => {
      if (document.hidden) setIsPlaying(false);
    };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => document.removeEventListener("visibilitychange", handleVisibility);
  }, []);

  useEffect(() => {
    if (!isVisible) setIsPlaying(false);
  }, [isVisible]);

  useEffect(() => {
    const safeFrameIndex = clampFrameIndex(frameIndex, frames.length);
    frameIndexRef.current = safeFrameIndex;
    if (safeFrameIndex !== frameIndex) setFrameIndex(safeFrameIndex);
    const canvas = canvasRef.current;
    const frame = frames[safeFrameIndex];
    if (canvas && frame) drawFrame(canvas, frame, profile.width, profile.height);
  }, [frameIndex, frames, profile.height, profile.width]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const redraw = () => {
      const safeFrameIndex = clampFrameIndex(frameIndexRef.current, frames.length);
      frameIndexRef.current = safeFrameIndex;
      const frame = frames[safeFrameIndex];
      if (frame) drawFrame(canvas, frame, profile.width, profile.height);
    };
    const observer = new ResizeObserver(redraw);
    observer.observe(canvas);
    redraw();
    return () => observer.disconnect();
  }, [frames, profile.height, profile.width]);

  useEffect(() => {
    if (!isPlaying || staticPreview || !isVisible) return;
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
  }, [isPlaying, isVisible, lastFrame, staticPreview]);

  const safeFrameIndex = clampFrameIndex(frameIndex, frames.length);
  const terminalState = getTerminalSceneState(safeFrameIndex);
  const phoneState = getPhoneSceneState(safeFrameIndex);
  const caption = getSceneCaption(safeFrameIndex);
  const overlay = getSceneOverlayLayout(profileKey, safeFrameIndex / REMOTE_SCENE_DATA.fps);
  const workProgress = Math.max(0, Math.min(1, (safeFrameIndex - 36) / (124 - 36)));
  const overlayStyle = {
    "--monitor-left": `${overlay.monitor.left}%`,
    "--monitor-top": `${overlay.monitor.top}%`,
    "--monitor-width": `${overlay.monitor.width}%`,
    "--monitor-height": `${overlay.monitor.height}%`,
    "--phone-left": `${overlay.phone.left}%`,
    "--phone-top": `${overlay.phone.top}%`,
    "--phone-width": `${overlay.phone.width}%`,
    "--phone-height": `${overlay.phone.height}%`,
    "--work-progress": workProgress,
    aspectRatio: `${profile.width} / ${profile.height}`,
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
    <section ref={playerRef} className="remote-scene-player" aria-label="Remote control scene player">
      <div className={`remote-scene-stage${safeFrameIndex >= 124 ? " remote-scene-stage--complete" : ""}`} style={overlayStyle}>
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
            <div className="remote-scene-phone-heading"><strong>Longhouse</strong></div>
            <span className="remote-scene-phone-live">● {phoneState.status}</span>
            <span className="remote-scene-phone-message">{phoneState.message}</span>
            <span className="remote-scene-phone-detail">{phoneState.detail}</span>
            <div className="remote-scene-work-progress"><i /></div>
            <b>{phoneState.status === "DONE" ? "✓" : "›"}</b>
          </div>
          <div className="remote-scene-ambient-label" key={caption}>{caption}</div>
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
          <span>{prefersReducedMotion ? "Reduced motion preference detected" : `24 fps deterministic ${profileKey} render`}</span>
          <span>Frame {String(safeFrameIndex).padStart(3, "0")} / {lastFrame}</span>
        </div>
      </div>
    </section>
  );
}
