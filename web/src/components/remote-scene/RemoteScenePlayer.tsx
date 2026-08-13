import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useDemoSeed } from "../../lib/demoSimulation";
import { REMOTE_SCENE_DATA } from "./generated/sceneData";
import { clampFrameIndex } from "./sceneCodec";
import {
  getPhoneSceneState,
  getSceneCaption,
  getSceneOverlayLayout,
  type SceneProfileKey,
} from "./sceneSpec";
import { RecordedSceneTerminal } from "./RecordedSceneTerminal";
import { loadRemoteSceneFrames } from "./sceneFrameLoader";
import {
  getRemoteSceneWorkState,
  getRemoteScenePlaybackPosition,
  REMOTE_SCENE_LOOP_START_FRAME,
  REMOTE_SCENE_PLAYBACK_FRAME_COUNT,
  remoteSceneLoopProgress,
} from "./recordedTimeline";
import "../../styles/remote-scene-player.css";

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
  const absoluteFrameRef = useRef(0);
  const workCycleRef = useRef(0);
  const [profileKey, setProfileKey] = useState<SceneProfileKey>(() =>
    typeof window !== "undefined" && window.matchMedia("(max-width: 720px)").matches ? "mobile" : "desktop",
  );
  const [frameIndex, setFrameIndex] = useState(0);
  const [frames, setFrames] = useState<Uint8Array[]>([]);
  const [loadFailed, setLoadFailed] = useState(false);
  const [workCycle, setWorkCycle] = useState(0);
  const [isPlaying, setIsPlaying] = useState(true);
  const [staticPreview, setStaticPreview] = useState(false);
  const [prefersReducedMotion, setPrefersReducedMotion] = useState(false);
  const [isVisible, setIsVisible] = useState(true);
  const [isDocumentVisible, setIsDocumentVisible] = useState(() =>
    typeof document === "undefined" || !document.hidden,
  );
  const demoSeed = useDemoSeed();

  const profile = REMOTE_SCENE_DATA.profiles[profileKey];
  const sceneLastFrame = Math.max(0, frames.length - 1);

  useEffect(() => {
    let cancelled = false;
    setLoadFailed(false);
    void loadRemoteSceneFrames()
      .then((loaded) => {
        if (!cancelled) setFrames(loaded[profileKey]);
      })
      .catch(() => {
        if (!cancelled) setLoadFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [profileKey]);

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
    const handleVisibility = () => setIsDocumentVisible(!document.hidden);
    document.addEventListener("visibilitychange", handleVisibility);
    return () => document.removeEventListener("visibilitychange", handleVisibility);
  }, []);

  useEffect(() => {
    const safeFrameIndex = clampFrameIndex(frameIndex, REMOTE_SCENE_PLAYBACK_FRAME_COUNT);
    const sceneFrameIndex = clampFrameIndex(safeFrameIndex, frames.length);
    frameIndexRef.current = safeFrameIndex;
    if (safeFrameIndex !== frameIndex) setFrameIndex(safeFrameIndex);
    const canvas = canvasRef.current;
    const frame = frames[sceneFrameIndex];
    if (canvas && frame) drawFrame(canvas, frame, profile.width, profile.height);
  }, [frameIndex, frames, profile.height, profile.width]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const redraw = () => {
      const safeFrameIndex = clampFrameIndex(frameIndexRef.current, REMOTE_SCENE_PLAYBACK_FRAME_COUNT);
      const sceneFrameIndex = clampFrameIndex(safeFrameIndex, frames.length);
      frameIndexRef.current = safeFrameIndex;
      const frame = frames[sceneFrameIndex];
      if (frame) drawFrame(canvas, frame, profile.width, profile.height);
    };
    const observer = new ResizeObserver(redraw);
    observer.observe(canvas);
    redraw();
    return () => observer.disconnect();
  }, [frames, profile.height, profile.width]);

  useEffect(() => {
    if (!isPlaying || staticPreview || !isVisible || !isDocumentVisible || frames.length === 0) return;
    const startedAt = performance.now() - absoluteFrameRef.current * (1000 / REMOTE_SCENE_DATA.fps);
    let animationFrame = 0;
    const tick = (now: number) => {
      const rawFrame = Math.floor((now - startedAt) / (1000 / REMOTE_SCENE_DATA.fps));
      const position = getRemoteScenePlaybackPosition(rawFrame);
      const nextFrame = position.frameIndex;
      absoluteFrameRef.current = rawFrame;
      frameIndexRef.current = nextFrame;
      if (position.workCycle !== workCycleRef.current) {
        workCycleRef.current = position.workCycle;
        setWorkCycle(position.workCycle);
      }
      setFrameIndex(nextFrame);
      animationFrame = requestAnimationFrame(tick);
    };
    animationFrame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(animationFrame);
  }, [frames.length, isDocumentVisible, isPlaying, isVisible, staticPreview]);

  const safeFrameIndex = clampFrameIndex(frameIndex, REMOTE_SCENE_PLAYBACK_FRAME_COUNT);
  const sceneFrameIndex = Math.min(safeFrameIndex, sceneLastFrame);
  const workState = getRemoteSceneWorkState(safeFrameIndex, demoSeed, workCycle);
  const phoneState = safeFrameIndex < REMOTE_SCENE_LOOP_START_FRAME
    ? getPhoneSceneState(sceneFrameIndex)
    : workState.phase === "queued"
      ? { key: "queued", status: "NEXT", message: workState.message, detail: workState.detail }
      : workState.phase === "working"
        ? { key: "live", status: "LIVE", message: workState.message, detail: workState.detail }
        : { key: "complete", status: "DONE", message: workState.message, detail: workState.detail };
  const caption = safeFrameIndex < REMOTE_SCENE_LOOP_START_FRAME
    ? getSceneCaption(sceneFrameIndex)
    : workCycle === 0 && safeFrameIndex < REMOTE_SCENE_LOOP_START_FRAME + 18
      ? "simulated continuation"
    : workState.phase === "queued"
      ? "another instruction arrives"
      : workState.phase === "working"
        ? "work continues on studio-mac"
        : "ready for the next task";
  const overlay = getSceneOverlayLayout(profileKey, sceneFrameIndex / REMOTE_SCENE_DATA.fps);
  const loopProgress = remoteSceneLoopProgress(safeFrameIndex);
  const loopAngle = loopProgress * Math.PI * 2;
  const isSteadyScene = safeFrameIndex >= REMOTE_SCENE_LOOP_START_FRAME;
  const terminalReveal = Math.max(0, Math.min(1, (sceneFrameIndex - 56) / 24));
  const terminalTarget = profileKey === "desktop"
    ? { left: 40, top: 24, width: 39, height: 31 }
    : { left: 4, top: 27, width: 72, height: 49 };
  const terminalBounds = {
    left: overlay.monitor.left + (terminalTarget.left - overlay.monitor.left) * terminalReveal,
    top: overlay.monitor.top + (terminalTarget.top - overlay.monitor.top) * terminalReveal,
    width: overlay.monitor.width + (terminalTarget.width - overlay.monitor.width) * terminalReveal,
    height: overlay.monitor.height + (terminalTarget.height - overlay.monitor.height) * terminalReveal,
  };
  const overlayStyle = {
    "--phone-left": `${overlay.phone.left}%`,
    "--phone-top": `${overlay.phone.top}%`,
    "--phone-width": `${overlay.phone.width}%`,
    "--phone-height": `${overlay.phone.height}%`,
    "--work-progress": workState.progress,
    "--terminal-opacity": terminalReveal,
    "--terminal-left": `${terminalBounds.left}%`,
    "--terminal-top": `${terminalBounds.top}%`,
    "--terminal-width": `${terminalBounds.width}%`,
    "--terminal-height": `${terminalBounds.height}%`,
    "--room-breathe-x": `${isSteadyScene ? Math.sin(loopAngle) * 0.16 : 0}%`,
    "--room-breathe-y": `${isSteadyScene ? Math.cos(loopAngle) * 0.1 : 0}%`,
    "--room-breathe-scale": isSteadyScene ? 1.004 + Math.sin(loopAngle) * 0.0015 : 1,
    aspectRatio: `${profile.width} / ${profile.height}`,
  } as CSSProperties;

  return (
    <section
      ref={playerRef}
      className="remote-scene-player remote-scene-player--embedded"
      aria-label="Remote control scene player"
    >
      <div
        className={`remote-scene-stage remote-scene-stage--cutin${safeFrameIndex >= 124 ? " remote-scene-stage--complete" : ""}`}
        style={overlayStyle}
        data-scene-frame={sceneFrameIndex}
        data-playback-frame={safeFrameIndex}
        data-scene-frame-count={frames.length}
        data-scene-ready={frames.length > 0 ? "true" : loadFailed ? "error" : "false"}
        data-work-task={workState.id}
        data-work-phase={workState.phase}
        data-work-cycle={workCycle}
      >
        <div className="remote-scene-world">
          <canvas ref={canvasRef} aria-hidden="true" />
          {loadFailed ? <p className="remote-scene-load-error">Scene unavailable</p> : null}
          <RecordedSceneTerminal
            replaySecond={workState.replaySecond}
            timeline={workState.timeline}
            sourceLabel={safeFrameIndex < REMOTE_SCENE_LOOP_START_FRAME ? "recorded PTY" : "simulated continuation"}
          />
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
        <button
          type="button"
          className="remote-scene-embedded-toggle"
          onClick={() => setIsPlaying((playing) => !playing)}
          aria-label={isPlaying ? "Pause remote work scene" : "Play remote work scene"}
        >
          {isPlaying ? "Pause" : "Play"}
        </button>
      </div>
    </section>
  );
}
